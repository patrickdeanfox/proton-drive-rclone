# Roadmap

proton-drive-rclone is the first phase of a larger **privacy-first multi-cloud file management platform**. This document outlines the phased feature plan so future contributors understand the direction.

## Phase 1: Proton Drive Sync (Current)

Single-provider file sync between a Linux PC and Proton Drive via rclone.

**Status: Complete**

- [x] Bidirectional sync (bisync) and one-way push/pull
- [x] Web UI for managing sync configs, schedules, file browsing
- [x] Live sync progress with streaming rclone output
- [x] rclone mount support (FUSE)
- [x] CLI tools (pdrive) for terminal-based management
- [x] systemd integration for background sync
- [x] Sync conflict resolution (newer, larger, skip)
- [x] File organization rules (organize by type/date)
- [x] Duplicate detection (hash-based and name-based)
- [x] Security hardening (input validation, XSS prevention, subprocess sandboxing)

## Phase 2: Multi-Cloud Support

Add support for additional cloud storage providers while maintaining the privacy-first approach.

**Planned Features:**

- [ ] **Provider abstraction layer** — rclone integration behind a service interface so new backends (Google Drive, S3, OneDrive, Backblaze B2) can be added without touching business logic
- [ ] **Provider cards in UI** — Dashboard cards for each connected cloud provider
- [ ] **Cross-provider sync rules** — Sync folder A to Proton and folder B to S3
- [ ] **Per-provider configuration** — Bandwidth limits, transfer counts, exclude patterns per provider
- [ ] **Provider health monitoring** — Connectivity checks, quota tracking per provider

**Architecture Notes:**
- The current `RCLONE_REMOTE` config will expand to a `providers[]` array
- Sync configs already have per-config remote path support; extend with provider ID
- rclone already supports 70+ backends; the abstraction layer wraps rclone commands generically

## Phase 3: Intelligent File Management

Local AI-powered features that run entirely on the user's machine for privacy.

**Planned Features:**

- [ ] **Duplicate detection (enhanced)** — Perceptual hashing for near-duplicate images, fuzzy matching for similar documents
- [ ] **Local LLM-powered search** — Natural language file search using a local model (e.g., llama.cpp, Ollama)
- [ ] **AI-generated file summaries** — Auto-generate descriptions for documents, PDFs, images
- [ ] **Smart file organization** — AI-suggested folder structures based on content analysis
- [ ] **Per-file metadata & embeddings** — Vector embeddings stored locally for semantic search

**Architecture Notes:**
- Embeddings stored in a local SQLite/DuckDB database alongside file metadata
- Database schema should include: `file_id`, `provider_id`, `path`, `hash`, `embedding`, `metadata_json`, `tags`, `created_at`
- All AI processing runs locally — no data sent to external APIs
- Feature flags control which AI features are active

## Phase 4: Visual Intelligence

Privacy-preserving image and video analysis running locally.

**Planned Features:**

- [ ] **Facial recognition** — Local face detection and clustering (no cloud API)
- [ ] **Object/scene detection** — Tag photos by content automatically
- [ ] **OCR** — Extract text from images and scanned documents
- [ ] **Photo timeline** — Chronological view with smart grouping
- [ ] **Duplicate photo detection** — Perceptual hash-based near-duplicate finding

**Architecture Notes:**
- Use ONNX Runtime or similar for local model inference
- Face embeddings stored in the local database, never uploaded
- Models downloaded once and cached locally

## Phase 5: Collaboration & Sharing

Selective sharing features while maintaining privacy defaults.

**Planned Features:**

- [ ] **Selective sync rules** — User-defined rules for what syncs where
- [ ] **Audit logs** — Track all sync operations, file changes, and access patterns
- [ ] **Encrypted local backups** — Snapshot sync state with encryption
- [ ] **Share links** — Generate temporary share links via provider APIs
- [ ] **Multi-device sync** — Coordinate sync state across multiple machines

## Design Principles

1. **Privacy by default** — All AI processing is local. No data leaves the machine unless explicitly synced to a chosen cloud provider.
2. **Provider agnostic** — The abstraction layer should work with any rclone-supported backend.
3. **Offline first** — Core features work without internet. Cloud sync is an optional layer.
4. **Modular** — Each phase is independently useful. Users can stop at Phase 1 and still have a complete product.
5. **No vendor lock-in** — All data formats are open. Migrating between providers should be a config change, not a data migration.
