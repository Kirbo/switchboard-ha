#!/usr/bin/env bash
# CalVer release for the HA integration, mirroring the main Switchboard repo's scheme but with
# ISOLATED numbering (its own tags only): YYYY.M.MICRO — UTC year, month with no leading zero, and
# MICRO = (max published micro this month) + 1, resetting to 0 each month (2026.6.0, 2026.6.1, …).
#
# Releases only on release-worthy Conventional Commits since the last GitHub Release. Renders this
# version's notes with git-cliff (cliff.toml — same format as the app), syncs manifest.json to the
# version, tags, and cuts the GitHub Release whose body HACS shows as the update's release notes.
#
# Needs: gh (authenticated via GH_TOKEN), git-cliff (mise), python3. Run from the repo root in CI.
set -euo pipefail

emit() {
  [ -n "${GITHUB_OUTPUT:-}" ] && echo "$1=$2" >>"$GITHUB_OUTPUT"
  echo ">> $1=$2"
}

git fetch --tags --force >/dev/null 2>&1 || true

# Range start = last published Release tag (NOT the last tag); first release ever → whole history.
PREV=$(gh release list --limit 1 --json tagName --jq '.[0].tagName' 2>/dev/null || true)
if [ -n "$PREV" ]; then LOG_RANGE="${PREV}..HEAD"; else LOG_RANGE="HEAD"; fi

# Release-worthy? feat/fix/perf/revert, or ANY breaking change (`!:` subject or BREAKING CHANGE
# footer) in the range. Lenient (-i): a mis-cased type over-includes rather than missing a release.
RELEASABLE=false
if git log --format='%s' "$LOG_RANGE" | grep -Eqi '^(feat|fix|perf|revert)(\([^)]+\))?!?:|^[a-z]+(\([^)]+\))?!:'; then
  RELEASABLE=true
elif git log --format='%B' "$LOG_RANGE" | grep -Eq '^BREAKING[ -]CHANGE:'; then
  RELEASABLE=true
fi
echo "range '${LOG_RANGE}' -> RELEASABLE=${RELEASABLE}"

if [ "$RELEASABLE" != true ]; then
  echo "no release-worthy commits since ${PREV:-repo root} — nothing to release"
  emit releasable false
  exit 0
fi

Y=$(date -u +%Y)
M=$(date -u +%m | sed 's/^0*//')
PREFIX="v${Y}.${M}."
# MICRO from this month's existing tags (isolated: only this repo's tags).
N=$(git tag --list "${PREFIX}*" | sed "s|^${PREFIX}||" | grep -E '^[0-9]+$' | sort -n | tail -1 || true)
if [ -z "$N" ]; then N=0; else N=$((N + 1)); fi
VERSION="${Y}.${M}.${N}"
TAG="v${VERSION}"
echo "releasing ${TAG}"

# This version's notes (go-semantic-release format via cliff.toml). Drop a leading blank line.
if [ -n "$PREV" ]; then
  git-cliff --config cliff.toml --tag "$TAG" "${PREV}..HEAD" | sed '1{/^$/d;}' >UNRELEASED-CHANGELOG.md
else
  git-cliff --config cliff.toml --tag "$TAG" | sed '1{/^$/d;}' >UNRELEASED-CHANGELOG.md
fi
echo "===== release notes =====" && cat UNRELEASED-CHANGELOG.md && echo "========================="

# HACS displays the release tag as the version, but HA core + hassfest read manifest.json — keep
# them in lockstep so the installed integration reports the same version. Replace only the version
# value (preserve the rest of the file's formatting); VERSION is digits+dots, so safe in sed.
MANIFEST="custom_components/switchboard/manifest.json"
sed -i -E "s/(\"version\": \")[^\"]*(\")/\1${VERSION}\2/" "$MANIFEST"
grep -q "\"version\": \"${VERSION}\"" "$MANIFEST" || { echo "manifest version bump failed"; exit 1; }

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add custom_components/switchboard/manifest.json
git commit -m "chore(release): ${TAG} [skip ci]"
git tag "$TAG"
# Push the bump + tag. A GITHUB_TOKEN push triggers no workflow (plus [skip ci]) — no release loop.
git push origin "HEAD:${GITHUB_REF_NAME:-main}" "$TAG"

gh release create "$TAG" --title "$VERSION" --notes-file UNRELEASED-CHANGELOG.md

emit releasable true
emit version "$VERSION"
