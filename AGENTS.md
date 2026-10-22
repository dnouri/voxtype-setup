# Voxtype setup kit

A small, public configuration recipe for packaged Voxtype on KDE Plasma Wayland, not a fork or replacement daemon.

- Use packaged Voxtype commands and KDE desktop-entry facilities. Do not patch Voxtype or restart KWin.
- Default delivery is direct typing, with no clipboard driver or clipboard failure fallback. A small post-processing helper adds complete dictations to Klipper history through an explicitly accepted, non-atomic save/copy/restore round trip. Restore the prior supported plain-text clipboard, prefer detected newer copies, and skip unsupported/unreadable snapshots rather than flatten rich data. No application-specific profiles, live-stream fragments or new service.
- Do not claim atomic KDE history-only insertion exists: Plasma 6.3.6 exposes no supported API for it. Use packaged wl-clipboard and qdbus6, exact capture acknowledgement, bounded calls, guarded cleanup and private nonblocking serialization. State limitations and skipped cases plainly; Voxtype 1.0.1's built-in restoration is not the implementation. Never manipulate Klipper's database or silently archive transcripts elsewhere.
- Keep documentation generic: no personal paths, machine inventories, conversation excerpts, private recordings, or local task histories.
- CPU/GPU selection, language and model are operator choices. Do not assume a GPU vendor or AVX-512.
- The installer defaults to a dry run. Replacements require explicit permission and private backups. No implicit sudo, package/model downloads, group changes or service restarts.
- Automated tests must use temporary HOME/XDG directories and fake desktop commands. Never touch the real clipboard, microphone, user configuration or services. Explicitly requested integration experiments need their own compositor, session bus and HOME/XDG directories, synthetic data, and fail-closed socket isolation.
- Voice samples require explicit interactive confirmation. Keep audio, transcripts, weights, backups and task reports in ignored tmp/; never commit them.
- Voxtype 1.0.1 has no unlimited dictation sentinel: zero stops immediately. Document the preset's finite u32 ceiling and growing RAM usage honestly.
- Run make check before committing. Commit locally; do not push without confirmation.
