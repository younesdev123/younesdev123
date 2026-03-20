#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from base64 import b64decode
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_REST = "https://api.github.com"
API_GRAPHQL = "https://api.github.com/graphql"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"La variable d'environnement {name} est obligatoire.")
    return value


def parse_tokens() -> list[str]:
    tokens: list[str] = []
    raw_multi = os.getenv("GITHUB_STATS_TOKENS", "")
    fallback_token = os.getenv("GITHUB_STATS_TOKEN", "").strip()

    for line in raw_multi.splitlines():
        token = line.strip()
        if token:
            tokens.append(token)

    if fallback_token:
        tokens.append(fallback_token)

    deduped_tokens: list[str] = []
    seen_tokens: set[str] = set()
    for token in tokens:
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        deduped_tokens.append(token)

    if not deduped_tokens:
        raise RuntimeError("Aucun token GitHub exploitable n'a été fourni.")

    return deduped_tokens


def log(message: str) -> None:
    print(f"[stats] {message}", file=sys.stderr)


def github_request(url: str, token: str, method: str = "GET", data: dict[str, Any] | None = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "github-profile-stats-generator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = None
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=payload, headers=headers, method=method)

    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API HTTP {exc.code} sur {url}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Erreur réseau vers {url}: {exc}") from exc


def graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    payload = github_request(API_GRAPHQL, token, method="POST", data={"query": query, "variables": variables})
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL a retourné des erreurs: {payload['errors']}")
    return payload["data"]


def get_authenticated_login(token: str) -> str | None:
    try:
        user = github_request(f"{API_REST}/user", token)
    except RuntimeError:
        return None
    return user.get("login")


def get_authenticated_user(token: str) -> dict[str, Any] | None:
    try:
        return github_request(f"{API_REST}/user", token)
    except RuntimeError:
        return None


def fetch_all_repositories(username: str, token: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    auth_user = get_authenticated_user(token)
    auth_login = auth_user.get("login") if auth_user else None
    use_authenticated_endpoint = auth_login is not None and auth_login.lower() == username.lower()
    seen_repo_ids: set[int] = set()

    page = 1
    while True:
        if use_authenticated_endpoint:
            params = urlencode(
                {
                    "visibility": "all",
                    "affiliation": "owner,organization_member,collaborator",
                    "sort": "updated",
                    "per_page": 100,
                    "page": page,
                }
            )
            url = f"{API_REST}/user/repos?{params}"
        else:
            params = urlencode(
                {
                    "type": "owner",
                    "sort": "updated",
                    "per_page": 100,
                    "page": page,
                }
            )
            url = f"{API_REST}/users/{username}/repos?{params}"

        current_page = github_request(url, token)
        if not current_page:
            break

        for repo in current_page:
            repo_id = repo.get("id")
            if repo_id in seen_repo_ids:
                continue
            seen_repo_ids.add(repo_id)
            repos.append(repo)
        if len(current_page) < 100:
            break
        page += 1

    return repos


def fetch_accessible_repositories(token: str) -> list[tuple[dict[str, Any], str]]:
    repos: list[tuple[dict[str, Any], str]] = []
    seen_repo_ids: set[int] = set()
    page = 1

    while True:
        params = urlencode(
            {
                "visibility": "all",
                "affiliation": "owner,organization_member,collaborator",
                "sort": "updated",
                "per_page": 100,
                "page": page,
            }
        )
        url = f"{API_REST}/user/repos?{params}"
        current_page = github_request(url, token)
        if not current_page:
            break

        for repo in current_page:
            repo_id = repo.get("id")
            if repo_id in seen_repo_ids:
                continue
            seen_repo_ids.add(repo_id)
            repos.append((repo, token))

        if len(current_page) < 100:
            break
        page += 1

    return repos


def aggregate_languages(repositories: list[tuple[dict[str, Any], str]]) -> list[tuple[str, int]]:
    language_totals: dict[str, int] = defaultdict(int)

    for repo, token in repositories:
        if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
            continue

        languages_url = repo.get("languages_url")
        if not languages_url:
            continue

        try:
            repo_languages = github_request(languages_url, token)
        except RuntimeError:
            continue

        for language, size in repo_languages.items():
            language_totals[language] += int(size)

    return sorted(language_totals.items(), key=lambda item: item[1], reverse=True)


FRAMEWORK_RULES = {
    "package.json": {
        "dependencies": {
            "vue": "Vue",
            "nuxt": "Nuxt",
            "react": "React",
            "next": "Next.js",
            "express": "Express",
            "@nestjs/core": "NestJS",
            "socket.io": "Socket.IO",
            "fastify": "Fastify",
            "svelte": "Svelte",
            "@angular/core": "Angular",
        }
    },
    "composer.json": {
        "dependencies": {
            "laravel/framework": "Laravel",
            "symfony/symfony": "Symfony",
            "symfony/framework-bundle": "Symfony",
            "livewire/livewire": "Livewire",
        }
    },
    "requirements.txt": {
        "dependencies": {
            "django": "Django",
            "fastapi": "FastAPI",
            "flask": "Flask",
        }
    },
    "pyproject.toml": {
        "dependencies": {
            "django": "Django",
            "fastapi": "FastAPI",
            "flask": "Flask",
        }
    },
    "*.csproj": {
        "dependencies": {
            "Microsoft.AspNetCore.App": "ASP.NET",
            "Microsoft.NET.Sdk.Web": "ASP.NET",
            "Microsoft.AspNetCore": "ASP.NET",
        }
    },
}


def fetch_file_content(owner: str, repo_name: str, path: str, token: str) -> str | None:
    url = f"{API_REST}/repos/{owner}/{repo_name}/contents/{path}"
    try:
        payload = github_request(url, token)
    except RuntimeError:
        return None

    if isinstance(payload, list):
        return None

    content = payload.get("content")
    encoding = payload.get("encoding")
    if not content or encoding != "base64":
        return None

    try:
        return b64decode(content).decode("utf-8", errors="ignore")
    except Exception:
        return None


def fetch_repo_root(owner: str, repo_name: str, token: str) -> list[dict[str, Any]]:
    url = f"{API_REST}/repos/{owner}/{repo_name}/contents"
    try:
        payload = github_request(url, token)
    except RuntimeError:
        return []

    if not isinstance(payload, list):
        return []

    return payload


def detect_frameworks_in_package_json(content: str) -> set[str]:
    detected: set[str] = set()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return detected

    merged_dependencies: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            merged_dependencies.update(value)

    for dependency_name, framework_name in FRAMEWORK_RULES["package.json"]["dependencies"].items():
        if dependency_name in merged_dependencies:
            detected.add(framework_name)

    return detected


def detect_frameworks_in_composer_json(content: str) -> set[str]:
    detected: set[str] = set()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return detected

    merged_dependencies: dict[str, Any] = {}
    for key in ("require", "require-dev"):
        value = data.get(key)
        if isinstance(value, dict):
            merged_dependencies.update(value)

    for dependency_name, framework_name in FRAMEWORK_RULES["composer.json"]["dependencies"].items():
        if dependency_name in merged_dependencies:
            detected.add(framework_name)

    return detected


def detect_frameworks_in_requirements(content: str) -> set[str]:
    detected: set[str] = set()
    lines = [line.strip().lower() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
    for dependency_name, framework_name in FRAMEWORK_RULES["requirements.txt"]["dependencies"].items():
        if any(line.startswith(dependency_name) for line in lines):
            detected.add(framework_name)
    return detected


def detect_frameworks_in_pyproject(content: str) -> set[str]:
    detected: set[str] = set()
    lower_content = content.lower()
    for dependency_name, framework_name in FRAMEWORK_RULES["pyproject.toml"]["dependencies"].items():
        if dependency_name in lower_content:
            detected.add(framework_name)
    return detected


def detect_frameworks_in_csproj(content: str) -> set[str]:
    detected: set[str] = set()
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return detected

    xml_blob = ET.tostring(root, encoding="unicode").lower()
    for dependency_name, framework_name in FRAMEWORK_RULES["*.csproj"]["dependencies"].items():
        if dependency_name.lower() in xml_blob:
            detected.add(framework_name)
    return detected


def detect_frameworks(repositories: list[tuple[dict[str, Any], str]]) -> list[tuple[str, int]]:
    framework_totals: dict[str, int] = defaultdict(int)

    for repo, token in repositories:
        if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
            continue

        owner = repo.get("owner", {}).get("login")
        repo_name = repo.get("name")
        if not owner or not repo_name:
            continue

        detected: set[str] = set()
        root_entries = fetch_repo_root(owner, repo_name, token)
        root_file_names = {entry.get("name", "") for entry in root_entries if entry.get("type") == "file"}

        if "package.json" in root_file_names:
            content = fetch_file_content(owner, repo_name, "package.json", token)
            if content:
                detected.update(detect_frameworks_in_package_json(content))

        if "composer.json" in root_file_names:
            content = fetch_file_content(owner, repo_name, "composer.json", token)
            if content:
                detected.update(detect_frameworks_in_composer_json(content))

        if "requirements.txt" in root_file_names:
            content = fetch_file_content(owner, repo_name, "requirements.txt", token)
            if content:
                detected.update(detect_frameworks_in_requirements(content))

        if "pyproject.toml" in root_file_names:
            content = fetch_file_content(owner, repo_name, "pyproject.toml", token)
            if content:
                detected.update(detect_frameworks_in_pyproject(content))

        csproj_files = [name for name in root_file_names if name.endswith(".csproj")]
        for csproj_file in csproj_files[:3]:
            content = fetch_file_content(owner, repo_name, csproj_file, token)
            if content:
                detected.update(detect_frameworks_in_csproj(content))

        for framework_name in detected:
            framework_totals[framework_name] += 1

    return sorted(framework_totals.items(), key=lambda item: item[1], reverse=True)


def count_paginated_items(url: str, token: str) -> int:
    total = 0
    page = 1

    while True:
        separator = "&" if "?" in url else "?"
        current_page = github_request(f"{url}{separator}per_page=100&page={page}", token)
        if not current_page:
            break
        total += len(current_page)
        if len(current_page) < 100:
            break
        page += 1

    return total


def count_author_commits(username: str, repositories: list[tuple[dict[str, Any], str]], start_date: datetime) -> int:
    total_commits = 0

    for repo, token in repositories:
        if repo.get("archived") or repo.get("disabled"):
            continue

        owner = repo.get("owner", {}).get("login")
        name = repo.get("name")
        if not owner or not name:
            continue

        params = urlencode(
            {
                "author": username,
                "since": start_date.isoformat(),
            }
        )
        commits_url = f"{API_REST}/repos/{owner}/{name}/commits?{params}"

        try:
            repo_commit_count = count_paginated_items(commits_url, token)
        except RuntimeError:
            continue

        total_commits += repo_commit_count

    return total_commits


def fetch_stats(username: str, tokens: list[str]) -> dict[str, Any]:
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=365)

    query = """
    query ProfileStats($username: String!, $from: DateTime!, $to: DateTime!, $prQuery: String!, $issueQuery: String!) {
      user(login: $username) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
          }
          totalCommitContributions
        }
      }
      pullRequests: search(type: ISSUE, query: $prQuery) {
        issueCount
      }
      issues: search(type: ISSUE, query: $issueQuery) {
        issueCount
      }
    }
    """

    variables = {
        "username": username,
        "from": start_date.isoformat(),
        "to": end_date.isoformat(),
        "prQuery": f"author:{username} is:pr created:>={start_date.date().isoformat()}",
        "issueQuery": f"author:{username} is:issue created:>={start_date.date().isoformat()}",
    }

    primary_token = tokens[0]
    log(f"Tokens detectes: {len(tokens)}")
    data = graphql(query, variables, primary_token)
    user = data.get("user")
    if not user:
        raise RuntimeError(f"Utilisateur GitHub introuvable: {username}")

    repositories: list[tuple[dict[str, Any], str]] = []
    seen_repo_ids: set[int] = set()
    for index, token in enumerate(tokens, start=1):
        try:
            current_repositories = fetch_accessible_repositories(token)
            log(f"Token #{index}: {len(current_repositories)} repositories accessibles")
        except RuntimeError as exc:
            log(f"Token #{index}: erreur d'accès API : {exc}")
            continue

        for repo, repo_token in current_repositories:
            repo_id = repo.get("id")
            if repo_id in seen_repo_ids:
                continue
            seen_repo_ids.add(repo_id)
            repositories.append((repo, repo_token))

    if not repositories:
        log("Aucun repository accessible via la liste de tokens, repli sur le token principal")
        repositories = [(repo, primary_token) for repo in fetch_all_repositories(username, primary_token)]

    languages = aggregate_languages(repositories)
    frameworks = detect_frameworks(repositories)
    authored_commits = count_author_commits(username, repositories, start_date)
    log(f"Repositories uniques retenus: {len(repositories)}")
    log(f"Langages agrégés : {len(languages)}")
    log(f"Frameworks agrégés : {len(frameworks)}")
    log(f"Commits auteur agrégés : {authored_commits}")

    return {
        "username": username,
        "generated_at": end_date.strftime("%Y-%m-%d %H:%M UTC"),
        "period_label": "365 derniers jours",
        "commits": authored_commits,
        "commit_contributions": user["contributionsCollection"]["totalCommitContributions"],
        "contributions": user["contributionsCollection"]["contributionCalendar"]["totalContributions"],
        "pull_requests": data["pullRequests"]["issueCount"],
        "issues": data["issues"]["issueCount"],
        "languages": languages,
        "frameworks": frameworks,
        "repositories": len(repositories),
        "private_enabled": any(repo.get("private") for repo, _ in repositories),
    }


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def render_svg(stats: dict[str, Any]) -> str:
    width = 860
    height = 940

    colors = ["#4f8cff", "#35c759", "#ffcc4d", "#ff7a6b", "#a974ff"]
    top_languages = stats["languages"][:5]
    total_language_size = sum(size for _, size in top_languages) or 1
    top_frameworks = stats["frameworks"][:6]
    max_framework_count = max((count for _, count in top_frameworks), default=1)

    cards = [
        ("Commits sur 12 mois", compact_number(stats["commits"]) if stats["commits"] >= 10000 else str(stats["commits"]), "#4f8cff"),
        ("Activité globale", compact_number(stats["contributions"]), "#35c759"),
    ]

    card_svg: list[str] = []
    for index, (label, value, accent) in enumerate(cards):
        x = 40 + index * 390
        y = 172
        card_svg.append(
            f"""
            <g transform="translate({x},{y})">
              <rect width="350" height="92" rx="20" fill="#151922" stroke="#2a3140" />
              <rect x="0" y="0" width="350" height="92" rx="20" fill="url(#cardGlow)" opacity="0.18" />
              <rect x="18" y="20" width="7" height="52" rx="3.5" fill="{accent}" />
              <text x="42" y="35" fill="#8f9bb3" font-size="13" font-weight="600" letter-spacing="0.2">{escape(label)}</text>
              <text x="42" y="68" fill="#f7f9fc" font-size="30" font-weight="800">{escape(value)}</text>
            </g>
            """
        )

    language_svg: list[str] = []
    bar_x = 40
    bar_y = 330
    current_x = bar_x
    bar_width = 780
    bar_height = 16

    for index, (language, size) in enumerate(top_languages):
        segment_width = max(bar_width * size / total_language_size, 8)
        if index == len(top_languages) - 1:
            segment_width = bar_x + bar_width - current_x
        language_svg.append(
            f'<rect x="{current_x:.2f}" y="{bar_y}" width="{segment_width:.2f}" height="{bar_height}" fill="{colors[index % len(colors)]}" rx="6" />'
        )
        current_x += segment_width

    legend_svg: list[str] = []
    legend_y = 378
    for index, (language, size) in enumerate(top_languages):
        percent = (size / total_language_size) * 100
        x = 40 + (index % 2) * 390
        y = legend_y + (index // 2) * 40
        color = colors[index % len(colors)]
        legend_svg.append(
            f"""
            <g transform="translate({x},{y})">
              <circle cx="8" cy="8" r="8" fill="{color}" />
              <text x="24" y="12" fill="#c9d1d9" font-size="14" font-weight="600">{escape(language)}</text>
              <text x="290" y="12" fill="#8b949e" font-size="13" text-anchor="end">{percent:.1f}%</text>
            </g>
            """
        )

    if top_languages:
        languages_block = "".join(language_svg) + "".join(legend_svg)
    else:
        languages_block = """
        <text x="40" y="382" fill="#8b949e" font-size="14">
          Aucun langage pertinent détecté sur les repositories accessibles.
        </text>
        """

    framework_rows: list[str] = []
    framework_start_y = 565
    for index, (framework_name, count) in enumerate(top_frameworks):
        y = framework_start_y + index * 32
        bar_width = 250 * (count / max_framework_count)
        framework_rows.append(
            f"""
            <g transform="translate(40,{y})">
              <text x="0" y="13" fill="#c9d1d9" font-size="14" font-weight="600">{escape(framework_name)}</text>
              <rect x="190" y="0" width="250" height="12" rx="6" fill="#1b2230" />
              <rect x="190" y="0" width="{bar_width:.2f}" height="12" rx="6" fill="#4f8cff" />
              <text x="500" y="12" fill="#8f9bb3" font-size="13" text-anchor="end">{count} repo{"s" if count > 1 else ""}</text>
            </g>
            """
        )

    frameworks_block = "".join(framework_rows) if framework_rows else """
      <text x="40" y="585" fill="#8b949e" font-size="14">
        Aucun framework clairement détecté sur les fichiers manifestes analysés.
      </text>
    """

    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="GitHub profile statistics">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="860" y2="940" gradientUnits="userSpaceOnUse">
      <stop stop-color="#0b0f17" />
      <stop offset="0.52" stop-color="#101722" />
      <stop offset="1" stop-color="#0d1320" />
    </linearGradient>
    <linearGradient id="hero" x1="40" y1="34" x2="240" y2="34" gradientUnits="userSpaceOnUse">
      <stop stop-color="#f7f9fc" />
      <stop offset="1" stop-color="#8ab4ff" />
    </linearGradient>
    <linearGradient id="cardGlow" x1="0" y1="0" x2="350" y2="92" gradientUnits="userSpaceOnUse">
      <stop stop-color="#ffffff" />
      <stop offset="1" stop-color="#ffffff" stop-opacity="0" />
    </linearGradient>
  </defs>
  <rect width="{width}" height="{height}" rx="28" fill="url(#bg)" />
  <rect x="16" y="16" width="{width - 32}" height="{height - 32}" rx="20" fill="none" stroke="#273042" />
  <rect x="40" y="38" width="120" height="8" rx="4" fill="#4f8cff" opacity="0.9" />
  <style>
    text {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Ubuntu, "Helvetica Neue", Arial, sans-serif;
    }}
  </style>
  <text x="40" y="80" fill="#8f9bb3" font-size="13" font-weight="700" letter-spacing="1.4">GITHUB PROFILE OVERVIEW</text>
  <text x="40" y="118" fill="url(#hero)" font-size="32" font-weight="800">@{escape(stats["username"])}</text>
  {''.join(card_svg)}
  <text x="40" y="287" fill="#f7f9fc" font-size="24" font-weight="800">Langages dominants</text>
  {languages_block}
  <text x="40" y="525" fill="#f7f9fc" font-size="24" font-weight="800">Frameworks récurrents</text>
  {frameworks_block}
  <text x="40" y="900" fill="#69748c" font-size="12">Généré le {escape(stats["generated_at"])}</text>
  <text x="655" y="900" fill="#8ab4ff" font-size="12" font-weight="700">Made by younesdev123</text>
</svg>
"""


def main() -> None:
    username = require_env("PROFILE_USERNAME")
    tokens = parse_tokens()
    output_path = Path(os.getenv("STATS_OUTPUT", "generated/stats.svg"))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stats = fetch_stats(username, tokens)
    svg = render_svg(stats)
    output_path.write_text(svg, encoding="utf-8")


if __name__ == "__main__":
    main()
