//! Network-free replay of recorded events against recorded layouts.
//!
//! `neko-verify fixtures` captures real events (raw BCS + the server's own
//! JSON mirror + the authoritative on-chain layout) into
//! `tests/fixtures/<protocol>/`. This unit test replays them with zero
//! network access: only the decoder + the exact chain-recorded layout run,
//! and the decode must match the server's independent JSON. Offline fixture
//! replay is the deterministic core of the adapter parity guarantee.
#![allow(dead_code)]


pub type Json = serde_json::Value;

#[cfg(test)]
use prost::Message;
#[cfg(test)]
use sui_rpc::proto::sui::rpc::v2::DatatypeDescriptor;

/// Regenerate with `neko-verify fixtures ...` (see DESIGN.md §6).
const CETUS_SWAP_FIXTURES: &str = include_str!("../../tests/fixtures/cetus/swaps.json");

#[derive(serde::Deserialize)]
struct FixtureFile {
    comment: String,
    layouts: std::collections::HashMap<String, String>,
    events: Vec<FixtureEvent>,
}

#[derive(serde::Deserialize)]
struct FixtureEvent {
    event_type: String,
    checkpoint: u64,
    tx_digest: String,
    event_index: u32,
    contents_hex: String,
    server_json: Json,
}

fn hex_decode(s: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(s.len() / 2);
    let b = s.as_bytes();
    for i in (0..b.len()).step_by(2) {
        let hi = (b[i] as char).to_digit(16).unwrap() as u8;
        let lo = (b[i + 1] as char).to_digit(16).unwrap() as u8;
        out.push((hi << 4) | lo);
    }
    out
}

/// Regenerate with `neko-verify backfill-fixtures ...` (see DESIGN.md §9/§18).
const SUIPUMP_EVENT_FIXTURES: &str = include_str!("../../tests/fixtures/suipump/events.json");

#[cfg(test)]
fn replay_fixture(json: &str, label: &str) {
    let parsed: FixtureFile = serde_json::from_str(json).expect("fixture parses");
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("tokio rt");
    rt.block_on(async {
        assert!(!parsed.events.is_empty(), "fixture must contain captured events");

        // Offline resolver: seed only the recorded layouts; no Client is used.
        let client = crate::rpc::connect(
            "https://example.invalid:443",
            Some("not-a-real-chain"),
        )
        .expect("client construction is lazy");
        let res = crate::adapt::PackageResolver::new(client);
        for (key, b64) in &parsed.layouts {
            let bytes = hex_decode(b64);
            let desc = DatatypeDescriptor::decode(bytes.as_slice()).expect("layout decodes");
            res.seed(key.clone(), desc).await;
        }

        let mut checked = 0usize;
        for ev in &parsed.events {
            let root = crate::adapt::ty_from_event_type(&ev.event_type).expect("parses");
            let contents = hex_decode(&ev.contents_hex);
            let decoded = crate::adapt::decode(root, &contents, &res)
                .await
                .unwrap_or_else(|e| panic!("decode {}: {e:#}", ev.event_type));
            assert!(
                crate::adapt::numeric_equal(&decoded, &ev.server_json),
                "parity mismatch for {} @cp{} idx{}\n bcs: {}\n srv: {}",
                ev.event_type,
                ev.checkpoint,
                ev.event_index,
                decoded,
                ev.server_json
            );
            checked += 1;
        }
        assert_eq!(checked, parsed.events.len(), "all recorded events replayed ({label})");
    });
}

#[test]
fn cetus_swap_fixture_replay() {
    replay_fixture(CETUS_SWAP_FIXTURES, "cetus");
}

#[test]
fn suipump_events_fixture_replay() {
    replay_fixture(SUIPUMP_EVENT_FIXTURES, "suipump");
}