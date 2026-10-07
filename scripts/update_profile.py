#!/usr/bin/env python3
"""Refresh public GitHub projects and aggregate counts in the profile README.

Standard library only. PROFILE_STATS_TOKEN is optional and is never written or
logged. Private repository details are discarded after counting; they are never
included in the README. The token must cover ALL repositories owned by the user.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
USERNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")


class ProfileError(Exception):
    """A safe-to-display error, without secrets or private repository details."""


class GitHub:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = API_ROOT + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Algorythmice-profile-readme",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            # Never include the request, headers, response body or exception text.
            raise ProfileError(f"GitHub a refusé la requête (HTTP {error.code}).") from None
        except (URLError, TimeoutError, OSError):
            raise ProfileError("Impossible de joindre GitHub. Réessaie plus tard.") from None
        except (ValueError, UnicodeError):
            raise ProfileError("GitHub a renvoyé une réponse illisible.") from None

    def pages(self, path: str, params: dict[str, Any]):
        # Build each URL ourselves, so authorization never follows an external
        # pagination URL. A final empty page also handles exact multiples of 100.
        for page in range(1, 10001):
            batch = self.get(path, {**params, "per_page": 100, "page": page})
            if not isinstance(batch, list):
                raise ProfileError("Format de la liste de dépôts GitHub inattendu.")
            yield batch
            if len(batch) < 100:
                return
        raise ProfileError("La pagination GitHub n'a pas pu être terminée.")


def owned_by(repo: dict[str, Any], username: str) -> bool:
    owner = repo.get("owner")
    return isinstance(owner, dict) and str(owner.get("login", "")).casefold() == username.casefold()


def public_repositories(api: GitHub, username: str) -> list[dict[str, Any]]:
    public: dict[int, dict[str, Any]] = {}
    for batch in api.pages("/users/" + quote(username, safe="") + "/repos", {
        "type": "owner", "sort": "full_name", "direction": "asc",
    }):
        for repo in batch:
            if not isinstance(repo, dict) or not owned_by(repo, username) or repo.get("private") is not False:
                raise ProfileError("La réponse publique contient un dépôt inattendu.")
            if not isinstance(repo.get("id"), int) or not isinstance(repo.get("name"), str):
                raise ProfileError("Un dépôt public contient des données incomplètes.")
            public[repo["id"]] = repo
    return sorted(public.values(), key=lambda repo: (repo["name"].casefold(), repo["name"]))


def private_repository_count(api: GitHub, username: str) -> int:
    identity = api.get("/user")
    if not isinstance(identity, dict) or str(identity.get("login", "")).casefold() != username.casefold():
        raise ProfileError("PROFILE_STATS_TOKEN doit appartenir au compte du profil.")
    # Keep only numeric IDs for deduplication across pages, never names or URLs.
    private_ids: set[int] = set()
    for batch in api.pages("/user/repos", {
        "visibility": "private", "affiliation": "owner",
        "sort": "full_name", "direction": "asc",
    }):
        for repo in batch:
            if not isinstance(repo, dict) or not owned_by(repo, username) or repo.get("private") is not True:
                raise ProfileError("La réponse du compteur privé contient un dépôt inattendu.")
            if not isinstance(repo.get("id"), int):
                raise ProfileError("Le compteur privé contient des données incomplètes.")
            private_ids.add(repo["id"])
    count = len(private_ids)
    # Some authenticated-user responses expose the account-wide owned total.
    # When available, use it to catch tokens restricted to selected repositories.
    expected = identity.get("owned_private_repos")
    if isinstance(expected, int) and expected != count:
        raise ProfileError("Le jeton ne permet pas de compter tous les dépôts privés. Sélectionne All repositories dans ses autorisations.")
    return count


def markdown_text(value: Any) -> str:
    """Render API text as text, not Markdown links, HTML, or table structure."""
    text = " ".join(str(value or "").split())
    text = html.escape(text, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~])", r"\\\1", text)


def projects_block(username: str, repositories: list[dict[str, Any]], overrides: dict[str, Any] | None = None) -> str:
    if not repositories:
        return "Aucun dépôt public pour le moment. / No public repositories yet."
    rows = ["| Projet / Project | Langage / Language |", "| :--- | :--- |"]
    others = []
    for repo in repositories:
        # Derive the URL from the validated owner/name, not an API-supplied link.
        url = "https://github.com/" + quote(username, safe="") + "/" + quote(repo["name"], safe="")
        label = markdown_text(repo["name"])
        language = markdown_text(repo.get("language")) or "—"
        if repo.get("fork"):
            others.append(f"- [{label}]({url}) — fork")
            continue
        if repo["name"].casefold() == username.casefold():
            others.append(f"- [{label}]({url}) — profil / profile")
            continue
        if repo.get("archived"):
            label += " · archive"
        rows.append(f"| [{label}]({url}) | {language} |")
    if len(rows) == 2:
        rows = []
    if others:
        rows += ["", "<details>", f"<summary>Autres dépôts / Other repositories ({len(others)})</summary>", "", *others, "", "</details>"]
    return "\n".join(rows)


def stats_block(public_count: int, private_count: int | None, verified_on: str | None = None, private_live: bool = False) -> str:
    private = str(private_count) if private_count is not None else "Indisponible / Unavailable"
    lines = [
        f"**{public_count}** publics · **{private}** privés",
    ]
    if private_count is not None and verified_on:
        # This aggregate-only cache survives removal of a token without losing
        # the last verified count. No private name, ID, URL or metadata is saved.
        cached = json.dumps({"count": private_count, "verified_on": verified_on}, separators=(",", ":"))
        lines += ["", f"<!-- PRIVATE_STATS: {cached} -->"]
    return "\n".join(lines)


def replace_block(readme: str, name: str, content: str) -> str:
    start = f"<!-- {name}:START -->"
    end = f"<!-- {name}:END -->"
    if readme.count(start) != 1 or readme.count(end) != 1:
        raise ProfileError(f"README.md doit contenir exactement une paire de marqueurs {name}.")
    before, remainder = readme.split(start, 1)
    if end not in remainder:
        raise ProfileError(f"Les marqueurs {name} de README.md sont dans le mauvais ordre.")
    _, after = remainder.split(end, 1)
    return before + start + "\n" + content + "\n" + end + after


def read_config(root: Path) -> dict[str, Any]:
    path = root / "profile.json"
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError
        return config
    except (OSError, ValueError):
        raise ProfileError("profile.json est illisible ou son JSON est invalide.") from None


def read_username(root: Path, config: dict[str, Any] | None = None) -> str:
    username = os.environ.get("PROFILE_USER", "").strip()
    if not username:
        config = read_config(root) if config is None else config
        username = config.get("username", "")
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise ProfileError("Le nom de compte GitHub est manquant ou invalide.")
    return username


def cached_private_count(readme: str, config: dict[str, Any]) -> tuple[int | None, str | None]:
    matches = re.findall(r"<!-- PRIVATE_STATS: (\{[^\r\n]*\}) -->", readme)
    if len(matches) > 1:
        raise ProfileError("Le README contient plusieurs caches de compteur privé.")
    try:
        cached = json.loads(matches[0]) if matches else {
            "count": config.get("private_count_initial"),
            "verified_on": config.get("private_count_verified_on"),
        }
        count, verified_on = cached.get("count"), cached.get("verified_on")
        if count is None and verified_on is None:
            return None, None
        if type(count) is not int or count < 0 or not isinstance(verified_on, str):
            raise ValueError
        if date.fromisoformat(verified_on).isoformat() != verified_on:
            raise ValueError
        return count, verified_on
    except (ValueError, TypeError, AttributeError):
        raise ProfileError("Le compteur privé mémorisé doit contenir un nombre positif ou nul et une date AAAA-MM-JJ.") from None


def update(root: Path) -> bool:
    readme_path = root / "README.md"
    if not readme_path.is_file():
        raise ProfileError("README.md est absent : crée le README et ses marqueurs avant de lancer la mise à jour.")
    try:
        previous = readme_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ProfileError("Impossible de lire README.md en UTF-8.") from None
    # Validate structure before any network request, including overlapping blocks.
    for name in ("PROJECTS", "STATS"):
        replace_block(previous, name, "")
    intervals = [(previous.index(f"<!-- {name}:START -->"), previous.index(f"<!-- {name}:END -->")) for name in ("PROJECTS", "STATS")]
    if max(pair[0] for pair in intervals) < min(pair[1] for pair in intervals):
        raise ProfileError("Les blocs PROJECTS et STATS ne doivent pas se chevaucher.")
    config = read_config(root)
    username = read_username(root, config)
    public = public_repositories(GitHub(os.environ.get("GITHUB_TOKEN")), username)
    private_token = os.environ.get("PROFILE_STATS_TOKEN", "").strip()
    # Any configured-token/API failure aborts the whole update. Existing counts
    # remain intact rather than being replaced with zero or unavailable.
    if private_token:
        private_count = private_repository_count(GitHub(private_token), username)
        verified_on = datetime.now(timezone.utc).date().isoformat()
    else:
        private_count, verified_on = cached_private_count(previous, config)
    result = replace_block(previous, "PROJECTS", projects_block(username, public))
    result = replace_block(result, "STATS", stats_block(len(public), private_count, verified_on, bool(private_token)))
    if result == previous:
        return False
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=root, prefix=".README-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(result)
        os.replace(temporary, readme_path)
    except OSError:
        raise ProfileError("Impossible d'enregistrer README.md. La mise à jour a été interrompue.") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="Dossier contenant README.md et profile.json")
    arguments = parser.parse_args()
    try:
        changed = update(arguments.root.resolve())
    except ProfileError as error:
        print(f"Mise à jour annulée : {error}", file=sys.stderr)
        return 1
    print("README mis à jour." if changed else "README déjà à jour.")
    if not os.environ.get("PROFILE_STATS_TOKEN", "").strip():
        print("Le compteur privé conserve la dernière valeur datée disponible ; sa mise à jour automatique nécessite PROFILE_STATS_TOKEN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
