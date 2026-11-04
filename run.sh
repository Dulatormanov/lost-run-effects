#!/bin/sh
# Runs tests/test_lost_run_effects.py inside LiveKit Agents at the head of #8256. With --patched
# first, it runs them and tests/test_effect_prototype.py in a second clone, vendor/agents-patched,
# with patches/*.patch applied; there the R3 test and finding 3's four pass, which their strict
# xfails report as XPASS(strict), a failure.
set -e
cd "$(dirname "$0")"
PIN=317f826957239e255dd67dd162c83b4cac6b072b
DIR=vendor/agents
TESTS=tests/test_lost_run_effects.py
if [ "$1" = --patched ]; then shift; DIR=vendor/agents-patched; TESTS="$TESTS tests/test_effect_prototype.py"; fi
[ -d $DIR ] || git clone -q --depth 1 --revision "$PIN" --recurse-submodules --shallow-submodules https://github.com/livekit/agents $DIR
[ "$(git -C $DIR rev-parse HEAD)" = "$PIN" ] || git -C $DIR checkout -q "$PIN"
git -C $DIR submodule update -q --init --depth 1
if [ $DIR != vendor/agents ] && git -C $DIR diff --quiet; then git -C $DIR apply "$PWD"/patches/*.patch; fi
[ -d $DIR/.venv ] || (cd $DIR && uv sync --all-extras --dev)
cp $TESTS $DIR/tests/
cd $DIR && exec uv run pytest $TESTS -v -p no:cacheprovider "$@"
