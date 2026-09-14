use anyhow::{Context, Result};
use futures::StreamExt;
use sqlx::PgPool;
use sui_rpc::client::{CheckpointStreamFrame, CheckpointStreamRequest, CheckpointStreamStart, Delivery};
use sui_rpc::field::{FieldMask, FieldMaskUtil};
use sui_rpc::proto::sui::rpc::v2::Checkpoint;

use crate::config::{CheckpointStart, Settings};
use crate::metrics::SharedMetrics;
use crate::store::{self, RawCheckpoint};

const PROGRESS_NAME: &str = "live";

fn build_request(resume_seq: Option<u64>) -> CheckpointStreamRequest {
    // Explicit mask: top-level sequence_number/digest are required by the
    // SDK's List catch-up; bare "*" left List items empty on this endpoint.
    CheckpointStreamRequest::new()
        .with_read_mask(FieldMask::from_paths([
            "sequence_number",
            "digest",
            "summary",
            "signature",
            "contents",
            "transactions",
            "objects",
        ]))
        .with_start(match resume_seq {
            Some(seq) => CheckpointStreamStart::Checkpoint(seq),
            None => CheckpointStreamStart::Tip,
        })
        .with_delivery(Delivery::Subscribe)
}

/// Persistent live intake: SubscribeCheckpoints (with gap repair via List),
/// persisting every checkpoint + cursor atomically. Reconnect/rebuild loop.
pub async fn run(settings: &Settings, pool: PgPool, metrics: SharedMetrics) -> Result<()> {
    let client = crate::rpc::connect(&settings.endpoint, settings.chain_id.as_deref())?;

    let mut resume_seq: Option<u64> = match settings.start {
        CheckpointStart::Tip => None,
        // Cursors are inclusive; resume strictly after the persisted checkpoint.
        // Cursor 0 = nothing persisted yet → start at the live tip.
        CheckpointStart::Db => store::load_progress(&pool, PROGRESS_NAME)
            .await?
            .filter(|s| *s > 0)
            .map(|s| s as u64 + 1),
        CheckpointStart::Seq(n) => match n {
            0 => None,
            n => Some(n),
        },
    };
    tracing::info!(?resume_seq, "intake starting");

    // Fresh restart hint: stop rebuilding the request if the durable cursor
    // doesn't move (protects against a hot error loop).
    let mut reconnect_delay = tokio::time::Duration::from_secs(1);

    loop {
        tracing::debug!(?resume_seq, "opening checkpoint stream");
        let mut stream = Box::pin(client.stream_checkpoints(build_request(resume_seq)));

        let mut expected_next: Option<u64> = None;
        while let Some(item) = stream.next().await {
            match item {
                Ok(frame) => {
                    if let Some(expected) = expected_next {
                        if frame.cursor != expected {
                            tracing::warn!(
                                cursor = frame.cursor,
                                expected,
                                "checkpoint order anomaly (gap/regression)"
                            );
                            metrics.bump_gap();
                        }
                    }
                    expected_next = Some(frame.cursor + 1);

                    if let Err(e) = process_frame(&pool, &metrics, &frame).await {
                        tracing::error!(error = %e, "frame processing failed");
                        break;
                    }
                    reconnect_delay = tokio::time::Duration::from_secs(1);
                }
                Err(e) => {
                    tracing::warn!(error = %e, "stream item error; rebuilding");
                    break;
                }
            }
        }

        tracing::warn!(delay_ms = reconnect_delay.as_millis(), "stream ended; reconnecting");
        tokio::time::sleep(reconnect_delay).await;
        reconnect_delay = (reconnect_delay * 2).min(tokio::time::Duration::from_secs(30));

        // Always re-anchor on the durable cursor; `None` means start at tip.
        resume_seq = match store::load_progress(&pool, PROGRESS_NAME).await? {
            Some(s) => Some(s as u64 + 1),
            None => None,
        };
    }
}

async fn process_frame(
    pool: &PgPool,
    metrics: &SharedMetrics,
    frame: &CheckpointStreamFrame,
) -> Result<()> {
    metrics.bump_frames();

    let mut tx = pool.begin().await.context("begin tx")?;

    if let Some(cp) = frame.checkpoint.as_ref() {
        let row = checkpoint_row(cp, frame)?;
        if row.insert(&mut tx).await? {
            metrics.bump_checkpoint();
        } else {
            metrics.bump_duplicate();
        }
        metrics.record(frame.cursor, row.ts_ms);
    } else {
        metrics.record(frame.cursor, 0);
    }

    // Persist the cursor in the same transaction as the row (crash safety).
    store::save_progress_in(&mut tx, PROGRESS_NAME, frame.cursor as i64).await?;
    tx.commit().await.context("commit checkpoint tx")?;
    Ok(())
}

fn checkpoint_row(cp: &Checkpoint, frame: &CheckpointStreamFrame) -> Result<RawCheckpoint> {
    let summary = cp
        .summary
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("checkpoint frame missing summary"))?;

    let ts_ms = summary
        .timestamp
        .as_ref()
        .map(|t| t.seconds.saturating_mul(1000) + t.nanos as i64 / 1_000_000)
        .unwrap_or(0);

    Ok(RawCheckpoint {
        seq: frame.cursor as i64,
        digest: cp.digest.clone().unwrap_or_default(),
        previous_digest: summary.previous_digest.clone(),
        epoch: summary.epoch.unwrap_or(0) as i64,
        ts_ms,
        network_total_transactions: summary.total_network_transactions.map(|v| v as i64),
        // Raw protobuf-encoded v2 Checkpoint (+summary BCs, contents, tx, objects).
        data: prost::Message::encode_to_vec(cp),
    })
}