use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// Cheap process-local counters for the live path. (Prometheus export is a
/// later phase; this covers the "watermark lag / rate" health signal now.)
#[derive(Default)]
pub struct Metrics {
    pub frames: AtomicU64,
    pub checkpoints: AtomicU64,
    pub gaps: AtomicU64,
    pub duplicates: AtomicU64,
    pub last_seq: AtomicU64,
    pub last_ts_ms: AtomicU64,
    pub trades: AtomicU64,
    pub dlq: AtomicU64,
}

impl Metrics {
    pub fn bump_frames(&self) {
        self.frames.fetch_add(1, Ordering::Relaxed);
    }

    pub fn bump_checkpoint(&self) {
        self.checkpoints.fetch_add(1, Ordering::Relaxed);
    }

    pub fn bump_gap(&self) {
        self.gaps.fetch_add(1, Ordering::Relaxed);
    }

    pub fn bump_duplicate(&self) {
        self.duplicates.fetch_add(1, Ordering::Relaxed);
    }

    pub fn record(&self, seq: u64, ts_ms: i64) {
        self.last_seq.store(seq, Ordering::Relaxed);
        self.last_ts_ms.store(ts_ms as u64, Ordering::Relaxed);
    }

    pub fn bump_trade(&self) {
        self.trades.fetch_add(1, Ordering::Relaxed);
    }

    pub fn bump_dlq(&self) {
        self.dlq.fetch_add(1, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> (u64, u64, u64, u64, u64, u64) {
        let (frames, cps, gaps, dups, seq, ts) = self.snapshot6();
        (frames, cps, gaps, dups, seq, ts)
    }

    pub fn snapshot6(&self) -> (u64, u64, u64, u64, u64, u64) {
        (
            self.frames.load(Ordering::Relaxed),
            self.checkpoints.load(Ordering::Relaxed),
            self.gaps.load(Ordering::Relaxed),
            self.duplicates.load(Ordering::Relaxed),
            self.last_seq.load(Ordering::Relaxed),
            self.last_ts_ms.load(Ordering::Relaxed),
        )
    }
}

pub type SharedMetrics = Arc<Metrics>;

pub async fn periodic_log(metrics: SharedMetrics, interval: tokio::time::Duration) {
    let mut ticker = tokio::time::interval(interval);
    let mut last_frames = 0u64;
    let mut _last_seq = 0u64;
    loop {
        ticker.tick().await;
        let (frames, cps, gaps, dups, seq, _ts) = metrics.snapshot();
        let rate = frames.saturating_sub(last_frames) as f64 / interval.as_secs_f64();
        tracing::info!(
            frames,
            checkpoints = cps,
            rate_fps = format!("{rate:.2}"),
            gaps,
            duplicates = dups,
            trades = metrics.trades.load(Ordering::Relaxed),
            dlq = metrics.dlq.load(Ordering::Relaxed),
            seq,
            "live-intake"
        );
        last_frames = frames;
        _last_seq = seq;
    }
}