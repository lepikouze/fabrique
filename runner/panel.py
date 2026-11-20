"""Client HTTP du panneau (jeton Bearer du runner), avec quelques nouvelles tentatives sur les erreurs réseau."""
import logging, time
import httpx
from . import config

log = logging.getLogger("runner.panel")


class Panel:
    def __init__(self, base=None, token=None, transport=None):
        self.h = httpx.Client(base_url=base or config.PANEL_URL, timeout=30, transport=transport,
                              headers={"Authorization": f"Bearer {token or config.RUNNER_TOKEN}", "User-Agent": "fabrique-runner"})

    def _post(self, path, body, tries=4):
        for i in range(tries):
            try:
                r = self.h.post(path, json=body)
                if r.status_code >= 500:
                    raise httpx.HTTPError(f"{r.status_code}")
                return r
            except httpx.HTTPError as e:
                if i == tries - 1:
                    raise
                log.warning("panneau injoignable (%s), nouvel essai", e)
                time.sleep(2 ** i)

    def heartbeat(self, info):
        r = self._post("/api/runner/heartbeat", info)
        r.raise_for_status()
        return r.json()

    def claim(self):
        r = self._post("/api/runner/claim", {})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def event(self, ref, **ev):
        r = self._post(f"/api/runner/jobs/{ref}/event", ev)
        if r.status_code != 200:
            log.error("événement %s refusé pour %s : %s", ev.get("type"), ref, r.text[:200])
            return None
        return r.json()
