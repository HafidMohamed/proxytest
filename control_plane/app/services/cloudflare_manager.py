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
    result = subprocess.run(
        ["sudo"] + cmd, capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr


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
        Replace UFW rules for ports 80/443 so only Cloudflare IPs are allowed.

        Strategy:
          1. Delete all existing numbered rules for ports 80/443
          2. Re-add allow rules for each CF CIDR
          3. Add deny rules for 80/443 from any (catches non-CF traffic)

        NOTE: Port 8000 (control plane) is NOT touched here – lock that down
              separately to your own IPs in production.
        """
        try:
            self._ufw_reset_ports()
            all_cidrs = ips["ipv4"] + ips["ipv6"]
            for cidr in all_cidrs:
                _sudo(["ufw", "allow", "proto", "tcp", "from", cidr, "to", "any",
                       "port", "80,443", "comment", "cloudflare"])
            # Deny everything else on 80/443
            _sudo(["ufw", "deny", "80/tcp"])
            _sudo(["ufw", "deny", "443/tcp"])
            # Reload UFW
            _sudo(["ufw", "reload"])
            msg = f"UFW updated: {len(all_cidrs)} Cloudflare ranges allowed on 80/443"
            logger.info(msg)
            return True, msg
        except Exception as exc:
            logger.error("UFW update failed: %s", exc)
            return False, str(exc)

    def _ufw_reset_ports(self):
        """Delete all UFW rules touching port 80 or 443."""
        rc, stdout, _ = _sudo(["ufw", "status", "numbered"])
        if rc != 0:
            return
        # Collect rule numbers that mention port 80 or 443 (reversed so deletes don't shift indices)
        numbers = []
        for line in stdout.splitlines():
            if ("80" in line or "443" in line) and line.strip().startswith("["):
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
