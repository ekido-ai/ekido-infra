#!/usr/bin/env bash
# The release: which commit of each Ekido repo the servers run, from deploy.json in ekido-infra.
# Sourced by ekido-autodeploy.sh and bootstrap.sh. Everything is public, so nothing here holds a credential.
#
#   release_resolve <out.json> [infra-ref]   reads deploy.json from ekido-infra (main, or the given commit) and pins
#                                            every repo to a commit SHA. Cheap: git ls-remote and one small file.
#   release_id <resolved.json>               one short id for the whole set of commits (changes when any repo moves)
#   release_ci <resolved.json>               prints green, wait or red: every repo's listed checks on its commit
#   release_assemble <resolved.json> <dir>   downloads each repo at its commit and places it by its map, giving the
#                                            same layout as the old single repo (apps/..., workers/..., infra/server)

INFRA_REPO="${EKIDO_INFRA_REPO:-ekido-ai/ekido-infra}"
GH_API=(curl -fsS --max-time 30 -H "Accept: application/vnd.github+json" -H "User-Agent: ekido-autodeploy")

_sha_of() {  # _sha_of <owner/repo> <heads/x | tags/x | 40-char sha>
  local repo=$1 ref=$2 out
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then echo "$ref"; return; fi
  # For an annotated tag, ls-remote also lists the peeled commit (^{}); it comes last, so take the last line.
  out=$(git ls-remote "https://github.com/$repo" "refs/$ref" "refs/$ref^{}" | tail -1 | cut -f1)
  [[ "$out" =~ ^[0-9a-f]{40}$ ]] || { echo "cannot resolve $repo $ref" >&2; return 1; }
  echo "$out"
}

# Which environment this server is, from /etc/ekido/environment: "staging" (or the file absent) follows deploy.json, the
# branches. "live" follows deploy.live.json, exact commits that only move through a reviewed promotion pull request
# (tools/promote.py). Anything else is refused, never guessed.
EKIDO_ENV_FILE="${EKIDO_ENV_FILE:-/etc/ekido/environment}"
release_environment() {
  local env; env=$(tr -d '[:space:]' < "$EKIDO_ENV_FILE" 2>/dev/null || true)
  case "$env" in
    ''|staging) echo staging ;;
    live) echo live ;;
    *) echo "unknown environment '$env' in $EKIDO_ENV_FILE (use staging or live)" >&2; return 1 ;;
  esac
}
release_manifest_file() {
  local env; env=$(release_environment) || return 1
  if [ "$env" = live ]; then echo deploy.live.json; else echo deploy.json; fi
}

release_resolve() {
  local out=$1 infra_ref="${2:-heads/main}" infra_sha manifest mfile
  mfile=$(release_manifest_file) || return 1
  infra_sha=$(_sha_of "$INFRA_REPO" "$infra_ref") || return 1
  manifest=$(curl -fsSL --max-time 30 "https://raw.githubusercontent.com/$INFRA_REPO/$infra_sha/$mfile") || return 1
  local resolved='[]' name repo ref sha entry
  while IFS= read -r entry; do
    name=$(jq -r .name <<<"$entry"); repo=$(jq -r .repo <<<"$entry"); ref=$(jq -r .ref <<<"$entry")
    # The infra repo follows the commit the manifest was read from, unless the manifest pins it to an exact commit
    # (live does: infra is promoted like everything else).
    if [ "$repo" = "$INFRA_REPO" ] && [[ ! "$ref" =~ ^[0-9a-f]{40}$ ]]; then sha=$infra_sha; else sha=$(_sha_of "$repo" "$ref") || return 1; fi
    resolved=$(jq --argjson e "$entry" --arg sha "$sha" '. + [$e + {sha: $sha}]' <<<"$resolved")
  done < <(jq -c '.repos[]' <<<"$manifest")
  jq -n --argjson r "$resolved" '{repos: $r}' > "$out"
}

release_id() {
  jq -r '[.repos[] | "\(.name)=\(.sha)"] | join(",")' "$1" | sha256sum | cut -c1-12
}

release_ci() {
  local file=$1 entry repo sha need runs verdict overall=green
  while IFS= read -r entry; do
    need=$(jq -c .checks <<<"$entry"); [ "$need" = "[]" ] && continue
    repo=$(jq -r .repo <<<"$entry"); sha=$(jq -r .sha <<<"$entry")
    runs=$("${GH_API[@]}" "https://api.github.com/repos/$repo/commits/$sha/check-runs?per_page=100") || { echo wait; return; }
    verdict=$(jq -r --argjson need "$need" '
      [.check_runs[] | {name, status, conclusion}] as $runs
      | [$need[] as $n | ($runs | map(select(.name == $n)) | first)] as $mine
      | if any($mine[]; . == null or .status != "completed") then "wait"
        elif all($mine[]; .conclusion == "success") then "green"
        else "red" end' <<<"$runs")
    case "$verdict" in
      red) echo "red $repo@${sha:0:7}"; return ;;
      wait) overall="wait" ;;
    esac
  done < <(jq -c '.repos[]' "$file")
  echo "$overall"
}

release_assemble() {
  local file=$1 dest=$2 entry repo sha tmp from to
  mkdir -p "$dest"
  while IFS= read -r entry; do
    repo=$(jq -r .repo <<<"$entry"); sha=$(jq -r .sha <<<"$entry")
    tmp=$(mktemp -d)
    # mktemp makes the folder 0700; a "." map copies the folder's own mode with cp -a, which left apps/coordinator
    # and apps/platform unreadable to their services on 16 Nov (status=200/CHDIR). Code folders are 0755.
    chmod 755 "$tmp"
    curl -fsSL --max-time 180 "https://codeload.github.com/$repo/tar.gz/$sha" | tar -xz -C "$tmp" --strip-components=1 || { rm -rf "$tmp"; return 1; }
    while IFS=$'\t' read -r from to; do
      [ -e "$tmp/$from" ] || { echo "$repo@${sha:0:7} has no $from" >&2; rm -rf "$tmp"; return 1; }
      mkdir -p "$(dirname "$dest/$to")"
      if [ -d "$tmp/$from" ]; then mkdir -p "$dest/$to"; cp -a "$tmp/$from/." "$dest/$to/"; else cp -a "$tmp/$from" "$dest/$to"; fi
    done < <(jq -r '.map[] | @tsv' <<<"$entry")
    rm -rf "$tmp"
  done < <(jq -c '.repos[]' "$file")
}
