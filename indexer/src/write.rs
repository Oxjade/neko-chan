//! Bin-shared writer: runs normalization inside an already-open transaction
//! (the `neko-indexer` binary opens it after the raw row + live cursor commit).

use anyhow::Result;
use sqlx::{Postgres, Transaction};
use sui_rpc::proto::sui::rpc::v2::Event;

use neko_indexer::adapt::PackageResolver;

/// Normalize a checkpoint's adapted events and return the written tally.
/// Callers are responsible for progress cursors (`normalized`) and counters.
pub async fn publish_checkpoint_in(
    tx: &mut Transaction<'_, Postgres>,
    res: &PackageResolver,
    checkpoint: i64,
    ts_ms: i64,
    events: &[Event],
) -> Result<neko_indexer::norm::Written> {
    neko_indexer::norm::publish_checkpoint(tx, res, checkpoint, ts_ms, events)
        .await
        .map_err(|e| anyhow::anyhow!("{e:#}"))
} 