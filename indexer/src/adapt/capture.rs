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

/// Flatten every event the checkpoint reports. When the stream omits the
/// per-event transaction digest it is back-filled from the owning
/// `ExecutedTransaction`. When it omits `event_index`, the event's position
/// within its transaction is substituted — exactly Sui's canonical EventID
/// semantics `(tx_digest, event_seq)` — so multi-event transactions never
/// collide on the writer's natural key.
pub fn events_in(cp: &Checkpoint) -> Vec<Event> {
    collect(cp, false)
}

/// Same as [`events_in`] but keeps only events whose `event_type` matches an
/// active adapter venue — the hot live path uses this so untouched
/// checkpoints (the overwhelming majority) cost a type check, not a clone of
/// every BCS payload in the block.
pub fn adapted_events_in(cp: &Checkpoint) -> Vec<Event> {
    collect(cp, true)
}

fn collect(cp: &Checkpoint, adapt_only: bool) -> Vec<Event> {
    let mut out = Vec::new();
    for tx in &cp.transactions {
        let digest = tx.digest.clone();
        if let Some(evs) = tx.events.as_ref() {
            for (seq, ev) in evs.events.iter().enumerate() {
                if adapt_only
                    && !ev
                        .event_type
                        .as_deref()
                        .is_some_and(|t| super::Venue::from_event_type(t).is_some())
                {
                    continue;
                }
                let mut ev = ev.clone();
                if ev.transaction_digest.is_none() {
                    ev.transaction_digest = digest.clone();
                }
                if ev.event_index.is_none() {
                    ev.event_index = Some(seq as u32);
                }
                out.push(ev);
            }
        }
    }
    out
}