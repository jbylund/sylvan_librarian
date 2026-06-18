---
title: "HTTP Compression Negotiation: brotli, zstd, and gzip in a Falcon Middleware"
date: 2027-04-10
publishDate: 2027-04-10
tags: ["arcane-tutor", "python", "http", "compression", "falcon", "middleware"]
summary: "Serving compressed responses correctly is more than calling gzip.compress(). Covers Accept-Encoding negotiation, server-side priority (zstd → brotli → gzip), buffered vs. streaming code paths, the 200-byte skip threshold, Vary headers, and gzip determinism with mtime=0."
---

## Why compress JSON card responses

## Parsing Accept-Encoding

## Server-side priority: zstd first

## Ignoring client q= weights

## Two code paths: buffered vs. streaming

## The 200-byte skip threshold

## Vary: Accept-Encoding and CDN correctness

## mtime=0 for gzip determinism

## Compression ratios and latency in practice
