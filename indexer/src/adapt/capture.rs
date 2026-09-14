//! Event capture from checkpoint contents — the authoritative path.
//!
//! The Mainnet public-good endpoint does not serve `ListEvents` (filters only
//! yielded watermark frames, zero items, even over a live window — verified
//! 2026-09-14 via `neko-verify event-types`). Events therefore come out of
//! `Checkpoint.transactions[].events.events[]`, which `SubscribeCheckpoints`
//! already delivers and Phase 1 persists raw. Decoding here mirrors exactly
//! what the network emitted; nothing is re-typed.

use anyhow::Result;
use prost::Message;
use sui_rpc::proto::sui::rpc::v2::{Checkpoint, Event};

/// Decode a stored raw `Checkpoint` protobuf (the blob Phase 1 persists).
pub fn decode_checkpoint(data: &[u8]) -> Result<Checkpoint> {
    Ok(Checkpoint::decode(data)?)
}

/// Flatten every event the checkpoint reports, preserving transaction and
/// event position via the `Event` proto fields themselves. When the checkpoint
/// stream omits the per-event transaction digest, it is back-filled from the
/// owning `ExecutedTransaction` so events stay attributable.
pub fn events_in(cp: &Checkpoint) -> Vec<Event> {
    let mut out = Vec::new();
    for tx in &cp.transactions {
        let digest = tx.digest.clone();
        if let Some(evs) = tx.events.as_ref() {
            for ev in &evs.events {
                let mut ev = ev.clone();
                if ev.transaction_digest.is_none() {
                    ev.transaction_digest = digest.clone();
                }
                out.push(ev);
            }
        }
    }
    out
}