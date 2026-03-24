"""
Cloudflare IP Manager
======================

Responsibilities
----------------
1. Fetch the current Cloudflare IPv4 + IPv6 ranges from their public API
2. Write two nginx snippets:
     /etc/nginx/snippets/cloudflare-realip.conf
       → set_real_ip_from for each CF range so nginx sees the REAL visitor IP
         (not Cloudflare's edge IP) via the CF-Connecting-IP header
     /etc/nginx/snippets/cloudflare-allow.conf
       → allow each CF range, deny all others
         (stops anyone bypassing Cloudflare and hitting our server directly)
3. Rewrite UFW rules so ports 80 and 443 only accept packets from CF IPs
4. Persist the last-fetched IP list to disk so the app still works after a
   restart even if the Cloudflare API is temporarily unreachable

Why this matters
----------------
Without this layer:
  - Attackers can find your origin IP (via DNS history, certificate logs, etc.)
    and bypass Cloudflare to hammer the server directly.
  - nginx would log Cloudflare's edge IP instead of the real visitor IP.

With this layer:
  - Only Cloudflare's anycast IPs can reach ports 80/443. Every other source
    gets a TCP RST from the kernel before nginx even sees the packet.
  - nginx unwraps the real visitor IP from CF-Connecting-IP so logs, rate
    limiting, and geo-blocking all work correctly.

Cloudflare publishes their IP ranges at:
  https://www.cloudflare.com/ips-v4
  https://www.cloudflare.com/ips-v6
These change occasionally – run update_cloudflare_ips.sh via cron daily.
"""

import json
import logging
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from ..config import settings

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CF_IPV4_URL   = "https://www.cloudflare.com/ips-v4"
CF_IPV6_URL   = "https://www.cloudflare.com/ips-v6"
CF_IP_CACHE   = "/etc/nginx/cloudflare-ips.json"   # persisted on disk

# Snippet paths (included from nginx.conf)
CF_REALIP_SNIPPET = "/etc/nginx/snippets/cloudflare-realip.conf"
CF_ALLOW_SNIPPET  = "/etc/nginx/snippets/cloudflare-allow.conf"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_url(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "translation-proxy/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8").strip()


def _sudo(cmd: list[str], timeout: int = 30) -> Tuple[int, str, str]:
    """
    BUG FIX: Original had no try/except — if ufw is missing (FileNotFoundError)
    or any subprocess call times out, it would propagate unhandled and crash the
    uvicorn worker, producing a blank 500 Internal Server Error response.
    """
    try:
        result = subprocess.run(
            ["sudo"] + cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error("Command timed out after %ds: sudo %s", timeout, " ".join(cmd))
        return 1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError:
        logger.error("Command not found: %s — is it installed?", cmd[0])
        return 1, "", f"Command not found: {cmd[0]}"
    except Exception as exc:
        logger.exception("Unexpected error running sudo %s", " ".join(cmd))
        return 1, "", f"Unexpected error: {exc}"


def _write_file_sudo(path: str, content: str) -> None:
    """Write a file to a root-owned path using sudo tee."""
    result = subprocess.run(
        ["sudo", "tee", path],
        input=content,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to write {path}: {result.stderr}")


# ── Public API ────────────────────────────────────────────────────────────────

class CloudflareManager:

    def fetch_cloudflare_ips(self) -> dict:
        """
        Fetch fresh Cloudflare IP ranges from their API.
        Falls back to the cached version if the fetch fails.
        Returns {"ipv4": [...], "ipv6": [...], "fetched_at": "..."}
        """
        try:
            ipv4 = [ip for ip in _fetch_url(CF_IPV4_URL).splitlines() if ip]
            ipv6 = [ip for ip in _fetch_url(CF_IPV6_URL).splitlines() if ip]
            data = {
                "ipv4": ipv4,
                "ipv6": ipv6,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": "live",
            }
            # Persist to cache
            try:
                _write_file_sudo(CF_IP_CACHE, json.dumps(data, indent=2))
            except Exception as e:
                logger.warning("Could not cache CF IPs: %s", e)
            logger.info(
                "Fetched %d IPv4 + %d IPv6 Cloudflare ranges",
                len(ipv4), len(ipv6),
            )
            return data
        except Exception as exc:
            logger.warning("Live CF IP fetch failed (%s), using cache", exc)
            return self._load_cached_ips()

    def _load_cached_ips(self) -> dict:
        """Load IPs from the on-disk cache, or return hardcoded fallback."""
        if Path(CF_IP_CACHE).exists():
            try:
                data = json.loads(Path(CF_IP_CACHE).read_text())
                data["source"] = "cache"
                return data
            except Exception:
                pass
        logger.warning("No CF IP cache found – using hardcoded fallback list")
        return {
            "ipv4": [
                "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22",
                "103.31.4.0/22",   "141.101.64.0/18", "108.162.192.0/18",
                "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22",
                "198.41.128.0/17", "162.158.0.0/15",  "104.16.0.0/13",
                "104.24.0.0/14",   "172.64.0.0/13",   "131.0.72.0/22",
            ],
            "ipv6": [
                "2400:cb00::/32",  "2606:4700::/32",  "2803:f800::/32",
                "2405:b500::/32",  "2405:8100::/32",  "2a06:98c0::/29",
                "2c0f:f248::/32",
            ],
            "fetched_at": "fallback",
            "source": "fallback",
        }

    # ── Nginx snippets ────────────────────────────────────────────────────────

    def write_nginx_realip_snippet(self, ips: dict) -> str:
        """
        Write /etc/nginx/snippets/cloudflare-realip.conf
        Tells nginx to trust CF headers for real IP extraction.
        """
        lines = [
            "# Cloudflare real-IP configuration",
            "# Auto-generated – do not edit manually",
            f"# Updated: {ips.get('fetched_at', 'unknown')}",
            "",
            "# Trust CF-Connecting-IP header from Cloudflare IPs only",
            "real_ip_header     CF-Connecting-IP;",
            "real_ip_recursive  on;",
            "",
            "# IPv4",
        ]
        for cidr in ips["ipv4"]:
            lines.append(f"set_real_ip_from {cidr};")
        lines += ["", "# IPv6"]
        for cidr in ips["ipv6"]:
            lines.append(f"set_real_ip_from {cidr};")
        lines.append("")

        content = "\n".join(lines)
        _write_file_sudo(CF_REALIP_SNIPPET, content)
        logger.info("Written %s", CF_REALIP_SNIPPET)
        return CF_REALIP_SNIPPET

    def write_nginx_allow_snippet(self, ips: dict) -> str:
        """
        Write /etc/nginx/snippets/cloudflare-allow.conf
        Used in the default_server block to reject non-Cloudflare traffic.
        """
        lines = [
            "# Cloudflare IP allowlist",
            "# Auto-generated – do not edit manually",
            f"# Updated: {ips.get('fetched_at', 'unknown')}",
            "",
            "# Allow Cloudflare IPv4",
        ]
        for cidr in ips["ipv4"]:
            lines.append(f"allow {cidr};")
        lines += ["", "# Allow Cloudflare IPv6"]
        for cidr in ips["ipv6"]:
            lines.append(f"allow {cidr};")
        lines += [
            "",
            "# Allow localhost (health checks from the same machine)",
            "allow 127.0.0.1;",
            "allow ::1;",
            "",
            "# Drop everything else – protects our origin IP",
            "deny all;",
            "",
        ]

        content = "\n".join(lines)
        _write_file_sudo(CF_ALLOW_SNIPPET, content)
        logger.info("Written %s", CF_ALLOW_SNIPPET)
        return CF_ALLOW_SNIPPET

    # ── UFW firewall ──────────────────────────────────────────────────────────

    def update_ufw_rules(self, ips: dict) -> Tuple[bool, str]:
        """
        Lock port 443 to Cloudflare IPs only.
        Port 80 stays open to ALL IPs — required for Let's Encrypt HTTP-01
        ACME challenges which come from LE's own servers, not Cloudflare.

        Architecture:
          port 80  → open to all  (ACME challenge + HTTP→HTTPS redirect only)
          port 443 → CF IPs only  (all real visitor traffic)
          port 8000 → untouched   (control plane — restrict manually in prod)
        """
        try:
            self._ufw_reset_https_only()
            all_cidrs = ips["ipv4"] + ips["ipv6"]
            # Allow port 443 from each Cloudflare CIDR
            for cidr in all_cidrs:
                _sudo(["ufw", "allow", "proto", "tcp", "from", cidr, "to", "any",
                       "port", "443", "comment", "cloudflare-https"])
            # Deny all other direct connections to 443
            _sudo(["ufw", "deny", "443/tcp"])
            # Port 80 MUST stay fully open for Let's Encrypt ACME HTTP-01 challenges
            # It is safe: port 80 only serves /.well-known/acme-challenge/ + redirects
            _sudo(["ufw", "allow", "80/tcp"])
            _sudo(["ufw", "reload"])
            msg = (
                f"UFW updated: port 443 locked to {len(all_cidrs)} Cloudflare ranges. "
                f"Port 80 open to all (required for ACME/Let's Encrypt)."
            )
            logger.info(msg)
            return True, msg
        except Exception as exc:
            logger.error("UFW update failed: %s", exc)
            return False, str(exc)

    def _ufw_reset_https_only(self):
        """Delete only UFW rules touching port 443 (leave port 80 alone)."""
        rc, stdout, _ = _sudo(["ufw", "status", "numbered"])
        if rc != 0:
            return
        numbers = []
        for line in stdout.splitlines():
            if "443" in line and line.strip().startswith("["):
                try:
                    num = int(line.strip().split("]")[0].lstrip("["))
                    numbers.append(num)
                except ValueError:
                    pass
        for num in sorted(numbers, reverse=True):
            _sudo(["ufw", "--force", "delete", str(num)])

    # ── Combined refresh ──────────────────────────────────────────────────────

    def full_refresh(self, update_ufw: bool = True) -> dict:
        """
        Fetch fresh CF IPs → update nginx snippets → optionally update UFW.
        Returns a status dict.
        """
        ips       = self.fetch_cloudflare_ips()
        realip    = self.write_nginx_realip_snippet(ips)
        allowlist = self.write_nginx_allow_snippet(ips)

        ufw_ok  = True
        ufw_msg = "UFW update skipped"
        if update_ufw:
            ufw_ok, ufw_msg = self.update_ufw_rules(ips)

        return {
            "ipv4_count":    len(ips["ipv4"]),
            "ipv6_count":    len(ips["ipv6"]),
            "source":        ips.get("source", "unknown"),
            "fetched_at":    ips.get("fetched_at"),
            "realip_snippet": realip,
            "allow_snippet":  allowlist,
            "ufw_updated":   ufw_ok,
            "ufw_message":   ufw_msg,
        }

    def get_status(self) -> dict:
        """Return cached CF IP info without fetching new data."""
        data = self._load_cached_ips()
        return {
            "ipv4_count": len(data["ipv4"]),
            "ipv6_count": len(data["ipv6"]),
            "source":     data.get("source", "unknown"),
            "fetched_at": data.get("fetched_at"),
            "realip_snippet_exists": Path(CF_REALIP_SNIPPET).exists(),
            "allow_snippet_exists":  Path(CF_ALLOW_SNIPPET).exists(),
        }
