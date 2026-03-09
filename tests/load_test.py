"""
Locust load test for the Translation Proxy control plane.
==========================================================

Install:   pip install locust
Run:       locust -f tests/load_test.py --host=http://localhost:8000

Or headless (CI):
    locust -f tests/load_test.py \
           --host=http://localhost:8000 \
           --users 50 --spawn-rate 10 \
           --run-time 60s --headless \
           --csv /tmp/proxy_load_results

Then check /tmp/proxy_load_results_stats.csv for failure rate.

Expected results on a modest server (2 vCPU, 4 GB RAM):
  - /health:           ~5000 req/s,  0% errors
  - /domains (GET):    ~800  req/s,  <0.1% errors
  - /customers (POST): ~400  req/s,  <0.1% errors
"""

import uuid
import os
from locust import HttpUser, task, between, events


API_KEY = os.getenv("LOAD_TEST_API_KEY", "")  # set this before running


class ProxyControlPlaneUser(HttpUser):
    """Simulates a dashboard user managing their domains."""

    wait_time = between(0.1, 0.5)   # think time between requests
    api_key   = ""
    domain    = ""

    def on_start(self):
        """Create a customer and pre-register a domain."""
        email = f"loadtest-{uuid.uuid4().hex[:10]}@example.com"
        with self.client.post(
            "/customers",
            json={"email": email},
            catch_response=True,
            name="/customers [setup]",
        ) as resp:
            if resp.status_code == 201:
                self.api_key = resp.json()["api_key"]
                resp.success()
            else:
                resp.failure(f"Setup failed: {resp.text}")
                self.api_key = ""
                return

        self.domain = f"loadtest-{uuid.uuid4().hex[:8]}.example.com"
        with self.client.post(
            "/domains",
            json={"domain": self.domain, "backend_url": "https://origin.example.com"},
            headers={"X-API-Key": self.api_key},
            catch_response=True,
            name="/domains [setup]",
        ) as resp:
            if resp.status_code != 201:
                resp.failure(f"Domain setup failed: {resp.text}")

    @task(10)
    def health_check(self):
        self.client.get("/health", name="/health")

    @task(5)
    def list_domains(self):
        if not self.api_key:
            return
        self.client.get(
            "/domains",
            headers={"X-API-Key": self.api_key},
            name="/domains [list]",
        )

    @task(3)
    def get_domain(self):
        if not self.api_key or not self.domain:
            return
        self.client.get(
            f"/domains/{self.domain}",
            headers={"X-API-Key": self.api_key},
            name="/domains/{domain} [get]",
        )

    @task(2)
    def nginx_status(self):
        self.client.get("/nginx/status", name="/nginx/status")

    @task(1)
    def verify_domain(self):
        """Trigger verification (will fail DNS check but tests endpoint resilience)."""
        if not self.api_key or not self.domain:
            return
        with self.client.post(
            f"/domains/{self.domain}/verify",
            headers={"X-API-Key": self.api_key},
            catch_response=True,
            name="/domains/{domain}/verify",
        ) as resp:
            # A 200 with "failed" in body is still a successful response
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}")


class ReadOnlyAnonymousUser(HttpUser):
    """Simulates unauthenticated traffic (monitoring, bots)."""

    wait_time = between(0.05, 0.2)

    @task
    def health_check(self):
        self.client.get("/health")

    @task
    def nginx_status(self):
        self.client.get("/nginx/status")


# ── Post-test stats printer ───────────────────────────────────────────────────

@events.quitting.add_listener
def on_quit(environment, **kwargs):
    stats = environment.stats
    total = stats.total
    if total.num_requests == 0:
        print("\n[LoadTest] No requests recorded.")
        return

    error_pct = total.num_failures / total.num_requests * 100
    print("\n" + "=" * 60)
    print("  LOAD TEST SUMMARY")
    print("=" * 60)
    print(f"  Total requests : {total.num_requests:,}")
    print(f"  Failures       : {total.num_failures:,}")
    print(f"  Error rate     : {error_pct:.2f}%")
    print(f"  Avg RPS        : {total.current_rps:.1f}")
    print(f"  Median latency : {total.median_response_time:.0f} ms")
    print(f"  95th pct       : {total.get_response_time_percentile(0.95):.0f} ms")
    print(f"  99th pct       : {total.get_response_time_percentile(0.99):.0f} ms")
    print("=" * 60)

    if error_pct > 1:
        print(f"\n  ⚠️  Error rate {error_pct:.2f}% exceeds 1% threshold!")
        environment.process_exit_code = 1
    else:
        print(f"\n  ✅ Error rate {error_pct:.2f}% is within acceptable limits.")
