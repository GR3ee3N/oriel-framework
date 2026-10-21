#!/bin/bash
# tools/release.sh "Release notes"
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
[[ $# = 1 && -n $1 ]] || { echo 'usage: tools/release.sh "Release notes"' >&2; exit 2; }
[[ -z ${WEBKIT_DEV:-} ]] || { echo "releases require the published WebKit runtime; unset WEBKIT_DEV" >&2; exit 2; }
[[ -z ${DISPLAY_DEV:-} ]] || { echo "releases require the published display protocol; unset DISPLAY_DEV" >&2; exit 2; }
export GH_REPO=GR3ee3N/oriel-framework
set -a
source build/versions.env
set +a
TAG=v$VERSION
COMMIT=$(git rev-parse HEAD)
[[ -z $(git status --porcelain) ]] || { echo "commit first; the tree is not clean" >&2; exit 2; }
mkdir -p .cache
exec 8>.cache/release.lock
flock -n 8 || { echo "another release is running" >&2; exit 1; }
git fetch -q origin "refs/tags/$TAG:refs/tags/$TAG" 2>/dev/null || true
RELEASE=$(gh release view "$TAG" --json isDraft -q .isDraft 2>/dev/null || true)
if [[ $RELEASE != true ]] && git rev-parse -q --verify "refs/tags/$TAG" >/dev/null \
    && [[ $(git rev-parse "$TAG^{commit}") != "$COMMIT" ]]; then
    echo "$TAG was released from another commit; bump VERSION in build/versions.env" >&2
    exit 2
fi
[[ $RELEASE != false ]] || { echo "$TAG is already released"; exit; }
WORK=$(mktemp -d "$REPO/.cache/release.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
git archive --prefix=oriel-framework/ "$COMMIT" > .cache/release-source.tar
tar -xf .cache/release-source.tar -C "$WORK"
ORIEL_CACHE="$REPO/.cache" "$WORK/oriel-framework/build/container.sh" build/release.sh "$COMMIT"
[[ $(git rev-parse HEAD) = "$COMMIT" && -z $(git status --porcelain) ]] \
    || { echo "the source changed during the release; commit and run again" >&2; exit 2; }
[[ $(cat .cache/release/COMMIT) = "$COMMIT" ]] \
    || { echo "release packages do not match the checked commit" >&2; exit 2; }
if [[ $RELEASE = true ]]; then
    [[ $(gh release view "$TAG" --json isDraft -q .isDraft) = true ]] \
        || { echo "$TAG is no longer a draft; check the release before retrying" >&2; exit 2; }
    # Keep the previous draft until its replacement has passed every check.
    gh release delete "$TAG" --yes --cleanup-tag
    if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then git tag -d "$TAG" >/dev/null; fi
fi
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || git tag -a "$TAG" "$COMMIT" -m "$1"
git push -q origin "$TAG"
gh release create "$TAG" --draft --verify-tag --title "Oriel framework $VERSION" --notes "$1" .cache/release/files/*
echo "Drafted $TAG; publish it on GitHub to release it."
