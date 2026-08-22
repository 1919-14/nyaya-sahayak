#!/usr/bin/env bash
# Render Build Script for Nyaya Sahayak
set -e

# Set writable temporary directories for Rust/Cargo/Maturin on Render's read-only builder
export CARGO_HOME=/tmp/cargo
export RUSTUP_HOME=/tmp/rustup
mkdir -p /tmp/cargo /tmp/rustup

# Upgrade pip and install pre-compiled binary wheels
pip install --upgrade pip setuptools wheel
pip install --prefer-binary -r requirements.txt

# Build vector database from ingested raw sources
python ingest/download_and_build.py --build-only
