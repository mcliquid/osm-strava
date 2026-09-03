#!/usr/bin/env python3
"""MapRoulette HTTP API client. No mr-cli. Never logs or returns the API key."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote

import requests

REPO = Path(__file__).resolve().parent
DEFAULT_API_URL = "https://maproulette.org"
DEFAULT_PROJECT_ID = 54842
DEFAULT_KEY_FILE = REPO / ".maproulette-api-key"

_TRANSIENT = {408, 429, 500, 502, 503, 504}


class MapRouletteError(Exception):
    """User-facing API error. Never includes the API key."""


class MapRouletteConfig:
    def __init__(
        self,
        api_url=None,
        api_key=None,
        project_id=None,
        timeout=30,
        upload_timeout=180,
        retries=3,
    ):
        self.api_url = (api_url or os.environ.get("MAPROULETTE_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.project_id = int(
            project_id
            or os.environ.get("MAPROULETTE_PROJECT_ID")
            or DEFAULT_PROJECT_ID
        )
        self.api_key = api_key if api_key is not None else load_api_key(required=False)
        self.timeout = timeout
        self.upload_timeout = upload_timeout
        self.retries = retries

    def has_api_key(self):
        return bool(self.api_key)


def load_api_key(required=True):
    env = os.environ.get("MAPROULETTE_API_KEY")
    if env and env.strip():
        return _normalize_api_key(env)
    try:
        raw = DEFAULT_KEY_FILE.read_bytes()
    except OSError:
        raw = b""
    key = _normalize_api_key(_decode_secret_file(raw))
    if key:
        return key
    if required:
        raise MapRouletteError(
            "MapRoulette-API-Schlüssel fehlt. Setze MAPROULETTE_API_KEY "
            "oder lege .maproulette-api-key im Projektverzeichnis an."
        )
    return None


def _decode_secret_file(raw):
    if not raw:
        return ""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    return raw.decode("utf-8", errors="replace")


def _normalize_api_key(value):
    if not value:
        return None
    text = value.replace("\ufeff", "").strip()
    if not text:
        return None
    text = text.splitlines()[0].strip().strip("\"'")
    if text.lower().startswith("apikey="):
        text = text.split("=", 1)[1].strip().strip("\"'")
    return text or None


def redact(text):
    if text is None:
        return ""
    out = str(text)
    candidates = [os.environ.get("MAPROULETTE_API_KEY") or ""]
    try:
        candidates.append(_normalize_api_key(_decode_secret_file(DEFAULT_KEY_FILE.read_bytes())) or "")
    except OSError:
        pass
    for key in candidates:
        if key:
            out = out.replace(key, "<redacted>")
    return out


class MapRouletteClient:
    def __init__(self, config=None):
        self.config = config or MapRouletteConfig()
        if not self.config.has_api_key():
            raise MapRouletteError(
                "MapRoulette-API-Schlüssel fehlt. Setze MAPROULETTE_API_KEY "
                "oder lege .maproulette-api-key im Projektverzeichnis an."
            )
        self.session = requests.Session()
        self.session.headers.update({
            "apiKey": self.config.api_key,
            "Accept": "application/json",
        })

    def close(self):
        self.session.close()

    def mapper_url(self, challenge_id):
        return f"{self.config.api_url}/browse/challenges/{int(challenge_id)}"

    def admin_url(self, challenge_id, project_id=None):
        pid = int(project_id or self.config.project_id)
        return f"{self.config.api_url}/admin/project/{pid}/challenge/{int(challenge_id)}"

    def _request(self, method, path, *, timeout=None, json_body=None, params=None, expected=(200, 201, 204, 304)):
        url = f"{self.config.api_url}/api/v2{path}"
        timeout = timeout if timeout is not None else self.config.timeout
        merged = dict(params or {})
        # Query-param auth survives proxies that drop the custom apiKey header.
        # redact() strips the key from any exception text.
        merged.setdefault("apiKey", self.config.api_key)
        last_error = None
        for attempt in range(self.config.retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    json=json_body,
                    params=merged,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_error = MapRouletteError(f"Netzwerkfehler bei {method} {path}: {redact(exc)}")
                if attempt < self.config.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_error from exc

            if response.status_code in expected:
                if not response.content or response.status_code in (204, 304):
                    return None
                try:
                    return response.json()
                except ValueError:
                    return response.text

            if response.status_code in _TRANSIENT and attempt < self.config.retries:
                time.sleep(1.5 * (attempt + 1))
                continue

            detail = redact(response.text[:400] if response.text else response.reason)
            raise MapRouletteError(
                f"MapRoulette {method} {path} fehlgeschlagen "
                f"(HTTP {response.status_code}): {detail}"
            )
        raise last_error or MapRouletteError(f"MapRoulette {method} {path} fehlgeschlagen")

    def get_project(self, project_id=None):
        pid = int(project_id or self.config.project_id)
        return self._request("GET", f"/project/{pid}")

    def verify_api_key(self):
        """Prove the key can authenticate. GET /project is public and is not sufficient."""
        managed = None
        for path in ("/projects/managed", "/user/secure"):
            try:
                managed = self._request("GET", path, expected=(200, 201))
                break
            except MapRouletteError as exc:
                if "HTTP 404" in str(exc):
                    continue
                if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                    raise MapRouletteError(
                        "MapRoulette-API-Schlüssel wurde nicht akzeptiert. "
                        "Bitte den Schlüssel unter https://maproulette.org/user/profile "
                        "prüfen oder neu erzeugen."
                    ) from exc
                raise
        if managed is None:
            raise MapRouletteError(
                "Konnte den API-Schlüssel nicht gegen einen authentifizierten Endpunkt prüfen."
            )
        project = self.get_project()
        if not isinstance(project, dict) or project.get("id") is None:
            raise MapRouletteError("Projekt nicht gefunden.")
        return {
            "project_id": project.get("id"),
            "project_name": project.get("name"),
            "enabled": project.get("enabled"),
            "authenticated": managed is not None,
        }

    def get_challenge(self, challenge_id):
        return self._request("GET", f"/challenge/{int(challenge_id)}")

    def find_challenge_by_name(self, name, project_id=None):
        pid = int(project_id or self.config.project_id)
        encoded = quote(name, safe="")
        try:
            return self._request(
                "GET",
                f"/project/{pid}/challenge/{encoded}",
                expected=(200, 201),
            )
        except MapRouletteError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def create_challenge(self, *, name, instruction, description="", blurb="", extra=None):
        body = {
            "name": name,
            "parent": self.config.project_id,
            "instruction": instruction,
            "description": description,
            "blurb": blurb,
            "enabled": False,
            "featured": False,
            "difficulty": 2,
            "defaultZoom": 14,
            "updateTasks": False,
        }
        if extra:
            body.update(extra)
        created = self._request("POST", "/challenge", json_body=body, expected=(200, 201))
        if not isinstance(created, dict) or created.get("id") is None:
            raise MapRouletteError("Challenge wurde erstellt, aber die Antwort enthält keine ID.")
        return created

    def update_challenge(self, challenge_id, fields):
        return self._request(
            "PUT",
            f"/challenge/{int(challenge_id)}",
            json_body=fields,
            expected=(200, 201, 204),
        )

    def add_tasks(self, challenge_id, feature_collection):
        """Upload an RFC 7946 FeatureCollection. One Point feature = one task."""
        if not isinstance(feature_collection, dict):
            raise MapRouletteError("add_tasks erwartet ein GeoJSON-Objekt.")
        if feature_collection.get("type") != "FeatureCollection":
            raise MapRouletteError("add_tasks erwartet type=FeatureCollection.")
        return self._request(
            "PUT",
            f"/challenge/{int(challenge_id)}/addTasks",
            json_body=feature_collection,
            timeout=self.config.upload_timeout,
            expected=(200, 201, 204, 304),
        )

    def list_tasks(self, challenge_id, *, limit=100, max_pages=50):
        tasks = []
        for page in range(max_pages):
            batch = self._request(
                "GET",
                f"/challenge/{int(challenge_id)}/tasks",
                params={"limit": limit, "page": page},
            )
            if not batch:
                break
            if isinstance(batch, dict):
                batch = batch.get("tasks") or batch.get("children") or []
            if not isinstance(batch, list) or not batch:
                break
            tasks.extend(batch)
            if len(batch) < limit:
                break
        return tasks

    def challenge_status(self, challenge_id):
        challenge = self.get_challenge(challenge_id)
        return {
            "id": challenge.get("id"),
            "name": challenge.get("name"),
            "status": challenge.get("status"),
            "statusMessage": challenge.get("statusMessage"),
            "enabled": challenge.get("enabled"),
            "parent": challenge.get("parent"),
            "mapper_url": self.mapper_url(challenge.get("id")),
            "admin_url": self.admin_url(challenge.get("id"), challenge.get("parent")),
        }

    def wait_until_ready(self, challenge_id, *, timeout_s=90, expected_tasks=None):
        """Poll until addTasks is no longer building. Does not modify the challenge."""
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            last = self.get_challenge(challenge_id)
            status = last.get("status")
            tasks = self.list_tasks(challenge_id, limit=50, max_pages=2)
            if expected_tasks is not None and len(tasks) >= expected_tasks:
                return last, tasks
            if status not in (None, 1) and expected_tasks is None:
                return last, tasks
            if status not in (None, 1) and expected_tasks is not None and tasks:
                return last, tasks
            time.sleep(2)
        return last, self.list_tasks(challenge_id, limit=50, max_pages=2)
