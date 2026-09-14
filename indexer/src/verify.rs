//! `neko-verify` — Phase 3 §6 on-chain verification CLI.
//!
//! Everything the adapters will trust is proved here against live chain data:
//!   1. package/address existence (GetPackage)
//!   2. real event type names (ListEvents → distinct event_type, never guessed)
//!   3. exact struct layouts (GetDatatype)
//!   4. BCS decode of real events through those layouts, cross-checked against
//!      the server's own `json` mirror.
//!
//! Usage:
//!   neko-verify package <package-id>
//!   neko-verify event-types <0xaddr::module> [limit]
//!   neko-verify datatype <package-id> <module> <name>
//!   neko-verify decode <0xaddr::mod::Event[<T>]> [limit]
//!   neko-verify cetus-swaps [limit]
//!   neko-verify backfill-scan <start-seq> [end-seq] <type-prefix> [sub] [max]

use std::collections::BTreeSet;
use std::env;

use anyhow::{Context, Result};
use futures::StreamExt;
use serde_json::{json, Value};
use sui_rpc::proto::sui::rpc::v2::{
    event_literal, transaction_literal, Event, EventFilter, EventLiteral, EmitModuleFilter,
    EventTerm, EventTypeFilter, TransactionFilter, TransactionLiteral, TransactionTerm,
};
use sui_rpc::proto::sui::rpc::v2::{
    ListEventsRequest, ListCheckpointsRequest, Ordering, QueryOptions,
};

use neko_indexer::adapt::*;

const CETUS_CLMM: &str = "0x25ebb9a7c50eb17b3fa9c5a30fb8b5ad8f97caaf4928943acbcff7153dfee5e3";
// Events are tagged with the *defining* (original) package id, not the
// current upgrade storage_id — confirmed live 2026-09-14.
const CETUS_CLMM_DEFINE: &str = "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb";

async fn client() -> Result<neko_indexer::rpc::Client> {
    let _ = dotenvy::dotenv();
    let endpoint =
        env::var("SUI_ENDPOINT").unwrap_or_else(|_| "https://fullnode.mainnet.sui.io:443".into());
    let chain_id = env::var("SUI_CHAIN_ID").ok();
    neko_indexer::rpc::connect(&endpoint, chain_id.as_deref())
}

fn plural(n: usize, word: &str) -> String {
    if n == 1 {
        format!("{n} {word}")
    } else {
        format!("{n} {word}s")
    }
}

fn emit_module_filter(module: &str) -> EventFilter {
    let mut mf = EmitModuleFilter::default();
    mf.module = Some(module.to_string());
    let mut lit = EventLiteral::default();
    lit.negated = false;
    lit.predicate = Some(event_literal::Predicate::EmitModule(mf));
    let mut term = EventTerm::default();
    term.literals = vec![lit];
    let mut f = EventFilter::default();
    f.terms = vec![term];
    f
}

fn event_type_filter(event_type: &str) -> EventFilter {
    let mut tf = EventTypeFilter::default();
    tf.event_type = Some(event_type.to_string());
    let mut lit = EventLiteral::default();
    lit.negated = false;
    lit.predicate = Some(event_literal::Predicate::EventType(tf));
    let mut term = EventTerm::default();
    term.literals = vec![lit];
    let mut f = EventFilter::default();
    f.terms = vec![term];
    f
}

/// Latest-N descending request (newest events first), used throughout verify.
fn latest_request(filter: EventFilter, limit: u32) -> ListEventsRequest {
    let mut opts = QueryOptions::default();
    opts.limit = Some(limit);
    opts.ordering = Some(Ordering::Descending as i32);
    let mut req = ListEventsRequest::default();
    req.filter = Some(filter);
    req.options = Some(opts);
    req
}

fn event_fields(e: &Event) -> Option<(String, String, String)> {
    let pkg = e.package_id.clone()?;
    let module = e.module.clone()?;
    let ty = e.event_type.clone()?;
    Some((pkg, module, ty))
}

fn module_filter(modules: &[&str]) -> TransactionFilter {
    let mut f = TransactionFilter::default();
    for module in modules {
        let mut mf = EmitModuleFilter::default();
        mf.module = Some(module.to_string());
        let mut lit = TransactionLiteral::default();
        lit.predicate = Some(transaction_literal::Predicate::EmitModule(mf));
        let mut term = TransactionTerm::default();
        term.literals = vec![lit];
        f.terms.push(term);
    }
    f
}

/// Scan a historical checkpoint range (start inclusive, end exclusive) via
/// `ListCheckpoints` and print every event whose `event_type` starts with
/// `prefix` and contains `sub`. `mod_filter` (full `0xaddr::module`) prunes
/// checkpoints server-side to only those emitting from that module.
/// Discovery only — reads straight from the ledger, nothing persisted.
/// `max` caps how many frames are scanned.
async fn cmd_backfill_scan(
    client: &neko_indexer::rpc::Client,
    start: u64,
    end: Option<u64>,
    prefix: &str,
    sub: &str,
    mod_filter: Option<&[&str]>,
    max_frames: Option<u64>,
) -> Result<()> {
    println!(
        "=== ListCheckpoints {start}..{} (event_type ~ starts_with {prefix} & contains {sub}) ===",
        end.map(|e| e.to_string()).unwrap_or_else(|| "tip".into())
    );
    let mut req = ListCheckpointsRequest::default();
    req.read_mask = Some(prost_types::FieldMask {
        paths: vec![
            "sequence_number".into(),
            "digest".into(),
            "summary".into(),
            "signature".into(),
            "contents".into(),
            "transactions".into(),
            "objects".into(),
        ],
    });
    req.start_checkpoint = Some(start);
    req.end_checkpoint = end;
    req.filter = mod_filter.map(module_filter);

    let mut stream = Box::pin(client.list_checkpoints(req));
    let mut frames = 0u64;
    let mut hits: Vec<(u64, String, String)> = Vec::new();
    let mut by_type: std::collections::BTreeMap<String, usize> = Default::default();
    while let Some(frame) = stream.next().await {
        let frame = frame.context("list_checkpoints frame")?;
        let Some(cp) = frame.checkpoint else { continue };
        frames += 1;
        if frames % 500 == 0 {
            println!("  ... {frames} checkpoints scanned");
        }
        if let Some(max) = max_frames {
            if frames > max {
                break;
            }
        }
        for ev in capture::events_in(&cp) {
            let ty = ev.event_type.clone().unwrap_or_default();
            if ty.starts_with(prefix) && ty.contains(sub) {
                let digest = ev.transaction_digest.clone().unwrap_or_default();
                let seq = cp.sequence_number.unwrap_or(0);
                hits.push((seq, digest, ty.clone()));
                *by_type.entry(ty).or_insert(0) += 1;
            }
        }
    }
    println!("{frames} checkpoints scanned; {} event hits", hits.len());
    if !by_type.is_empty() {
        println!("distinct event types:");
        for (ty, n) in &by_type {
            println!("   {n}x {ty}");
        }
    }
    for (seq, digest, ty) in hits {
        println!("   cp{seq} {digest} {ty}");
    }
    Ok(())
}

async fn cmd_package(res: &PackageResolver, id: &str) -> Result<()> {
    println!("=== GetPackage {id} ===");
    let pkg = res.package(id).await?;
    println!("storage_id    : {}", pkg.storage_id.clone().unwrap_or_default());
    println!("original_id   : {}", pkg.original_id.clone().unwrap_or_default());
    println!("version       : {}", pkg.version.map(|v| v.to_string()).unwrap_or_else(|| "?".into()));
    println!("modules       : {}", plural(pkg.modules.len(), "module"));
    for m in &pkg.modules {
        let name = m.name.clone().unwrap_or_default();
        let datatypes = m.datatypes.len();
        println!("  - {name} ({})", plural(datatypes, "datatype"));
        for d in &m.datatypes {
            println!("      • {} — {} type params", d.type_name.clone().unwrap_or_default(), d.type_parameters.len());
        }
    }
    Ok(())
}

async fn cmd_datatype(res: &PackageResolver, id: &str, module: &str, name: &str) -> Result<()> {
    let tag = type_tag::parse(&format!("{id}::{module}::{name}"))?;
    let desc = res.datatype(&tag).await?;
    println!("=== GetDatatype {}::{module}::{name} ===", short_id(id));
    println!("{}", serde_json::to_string_pretty(&descriptor_to_json(&desc))?);
    Ok(())
}

fn short_id(id: &str) -> &str {
    if id.len() > 12 { &id[..12] } else { id }
}

fn sig_to_json(body: &sui_rpc::proto::sui::rpc::v2::OpenSignatureBody) -> Value {
    use sui_rpc::proto::sui::rpc::v2::open_signature_body::Type;
    let kind = match body.r#type {
        Some(t) => Type::try_from(t).unwrap_or(Type::Unknown),
        None => Type::Unknown,
    };
    let base = match kind {
        Type::Unknown => "?".to_string(),
        Type::Address => "address".to_string(),
        Type::Bool => "bool".to_string(),
        Type::U8 => "u8".to_string(),
        Type::U16 => "u16".to_string(),
        Type::U32 => "u32".to_string(),
        Type::U64 => "u64".to_string(),
        Type::U128 => "u128".to_string(),
        Type::U256 => "u256".to_string(),
        Type::Vector => format!(
            "vector<{}>",
            body.type_parameter_instantiation
                .first()
                .map(sig_to_json)
                .and_then(|v| v.as_str().map(|s| s.to_string()))
                .unwrap_or_else(|| "?".into())
        ),
        Type::Datatype => {
            let name = body.type_name.clone().unwrap_or_default();
            if body.type_parameter_instantiation.is_empty() {
                name
            } else {
                let args = body
                    .type_parameter_instantiation
                    .iter()
                    .map(sig_to_json)
                    .filter_map(|v| v.as_str().map(|s| s.to_string()))
                    .collect::<Vec<_>>()
                    .join(", ");
                format!("{name}<{args}>")
            }
        }
        Type::Parameter => format!("T{}", body.type_parameter.unwrap_or_default()),
        _ => "?".to_string(),
    };
    json!(base)
}

fn descriptor_to_json(d: &sui_rpc::proto::sui::rpc::v2::DatatypeDescriptor) -> Value {
    let mut obj = serde_json::Map::new();
    obj.insert("type_name".into(), json!(d.type_name.clone().unwrap_or_default()));
    obj.insert("kind".into(), json!(if d.kind == Some(1) { "struct" } else if d.kind == Some(2) { "enum" } else { "?" }));
    let fields = d
        .fields
        .iter()
        .map(|f| {
            json!({
                "name": f.name.clone().unwrap_or_default(),
                "position": f.position.unwrap_or_default(),
                "type": f.r#type.as_ref().map(sig_to_json).unwrap_or_else(|| json!("?")),
            })
        })
        .collect::<Vec<_>>();
    obj.insert("fields".into(), json!(fields));
    Value::Object(obj)
}

async fn cmd_event_types(
    client: &neko_indexer::rpc::Client,
    module: &str,
    limit: u32,
    range: Option<(u64, u64)>,
) -> Result<()> {
    let label = if module == "*" { "(no filter)" } else { module };
    println!("=== ListEvents(emit_module={label}) — latest {limit} (range {:?}) ===", range);
    let filter = (module != "*").then(|| emit_module_filter(module));
    let req = {
        let mut opts = QueryOptions::default();
        opts.limit = Some(limit);
        if range.is_none() {
            opts.ordering = Some(Ordering::Descending as i32);
        }
        let mut r = ListEventsRequest::default();
        r.filter = filter;
        r.options = Some(opts);
        if let Some((start, end)) = range {
            r.start_checkpoint = Some(start);
            r.end_checkpoint = Some(end);
        }
        r
    };

    let mut seen: BTreeSet<String> = BTreeSet::new();
    let counts: std::collections::BTreeMap<String, usize> = Default::default();
    let mut by_count = counts;
    let mut frames = 0usize;
    let mut stream = Box::pin(client.list_events(req));
    while let Some(frame) = stream.next().await {
        match frame {
            Ok(f) => {
                frames += 1;
                if frames % 100 == 0 {
                    println!("  ... {frames} frames scanned");
                }
                if let Some(ev) = f.event {
                    if let Some((_, _, ty)) = event_fields(&ev) {
                        seen.insert(ty.clone());
                        *by_count.entry(ty).or_insert(0) += 1;
                    }
                }
            }
            Err(e) => {
                println!("  ! frame error: {e}");
                break;
            }
        }
    }
    println!("{frames} frames; {}", plural(seen.len(), "distinct event type"));
    for ty in &seen {
        println!("  {ty}  (x{})", by_count.get(ty).copied().unwrap_or_default());
    }
    Ok(())
}

async fn cmd_decode(client: &neko_indexer::rpc::Client, res: &PackageResolver, event_type: &str, limit: u32) -> Result<Vec<Value>> {
    println!("=== ListEvents(event_type={event_type}) — latest {limit} ===");
    let req = latest_request(event_type_filter(event_type), limit);

    let root_ty = ty_from_event_type(event_type)?;
    let mut decoded = Vec::new();
    let mut warnings = 0usize;
    let mut stream = Box::pin(client.list_events(req));
    while let Some(frame) = stream.next().await {
        let frame = frame.context("list_events frame")?;
        let Some(ev) = frame.event else { continue };
        let contents = ev
            .contents
            .as_ref()
            .and_then(|b| b.value.as_ref())
            .cloned()
            .map(|b| b.to_vec())
            .context("event missing BCS contents")?;

        match decode(root_ty.clone(), &contents, res).await {
            Ok(tree) => {
                let server_json = ev.json.as_ref().map(|b| proto_value_to_json(b.as_ref()));
                let parity = match &server_json {
                    Some(sj) => numeric_equal(&tree, sj),
                    None => true,
                };
                if !parity {
                    warnings += 1;
                    println!("  ! parity mismatch (server json differs from BCS decode)");
                    if warnings <= 3 {
                        println!("    bcs  : {}", serde_json::to_string(&tree)?);
                        if let Some(sj) = &server_json {
                            println!("    json : {}", serde_json::to_string(sj)?);
                        }
                    }
                }
                let digest = ev.transaction_digest.clone().unwrap_or_default();
                decoded.push(json!({
                    "event_type": event_type,
                    "checkpoint": ev.checkpoint.unwrap_or_default(),
                    "tx_digest": digest,
                    "event_index": ev.event_index.unwrap_or_default(),
                    "sender": ev.sender.clone().unwrap_or_default(),
                    "decoded": tree,
                    "parity_ok": parity,
                }));
            }
            Err(e) => {
                warnings += 1;
                println!("  ! decode failed: {e:#}");
            }
        }
    }

    println!(
        "decoded {} events ({} decode/parity issue(s))",
        decoded.len(),
        warnings
    );
    Ok(decoded)
}

/// §6 event proof against raw checkpoint data (authoritative; ListEvents is
/// not served on the public-good endpoint). Scans the last `n` raw
/// checkpoints, decodes every event whose package matches `pkg_prefix` and
/// whose event_type contains `sub`, then BCS-decodes through the on-chain
/// layout with a parity check against the server's own `json` mirror.
async fn cmd_db_scan(
    res: &PackageResolver,
    pkg_prefix: &str,
    sub: &str,
    n: u32,
    samples_per_type: usize,
) -> Result<()> {
    let url = env::var("DATABASE_URL").context("DATABASE_URL must be set (see .env.example)")?;
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(&url)
        .await
        .context("connect postgres")?;

    let rows: Vec<(i64, Vec<u8>)> = sqlx::query_as(
        "select seq, data from raw_checkpoints order by seq desc limit $1",
    )
    .bind(n as i64)
    .fetch_all(&pool)
    .await
    .context("query raw_checkpoints")?;
    if rows.is_empty() {
        anyhow::bail!("no raw checkpoints present — start the intake first");
    }

    // [seq -> events] with a soft cap on fetched/decoded work per checkpoint.
    let mut found: Vec<(i64, Vec<sui_rpc::proto::sui::rpc::v2::Event>)> = Vec::new();
    for (seq, data) in &rows {
        let cp = capture::decode_checkpoint(data).context("decode stored checkpoint")?;
        let evs: Vec<sui_rpc::proto::sui::rpc::v2::Event> = capture::events_in(&cp)
            .into_iter()
            .filter(|e| {
                e.event_type
                    .as_deref()
                    .is_some_and(|t| t.starts_with(pkg_prefix) && t.contains(sub))
            })
            .collect();
        if !evs.is_empty() {
            found.push((*seq, evs));
        }
    }

    let total = found.iter().map(|(_, e)| e.len()).sum::<usize>();
    println!(
        "db-scan: {} raw checkpoints ({}..{}); {total} matching {sub} event(s)",
        rows.len(),
        rows.last().map(|(s, _)| *s).unwrap_or(0),
        rows.first().map(|(s, _)| *s).unwrap_or(0),
    );
    if total == 0 {
        anyhow::bail!("no events matching prefix {pkg_prefix} / substring {sub} in window");
    }

    // Distinct event types (network-reported), decoded in type order.
    let mut by_type: std::collections::BTreeMap<String, Vec<Value>> = Default::default();
    let mut failures: Vec<String> = Vec::new();
    for (seq, evs) in &found {
        for ev in evs {
            let Some(ty) = ev.event_type.clone() else { continue };
            let contents = ev
                .contents
                .as_ref()
                .and_then(|b| b.value.as_ref())
                .cloned()
                .map(|b| b.to_vec())
                .unwrap_or_default();
            let root_ty = match ty_from_event_type(&ty) {
                Ok(t) => t,
                Err(e) => {
                    failures.push(format!("{ty}: type parse {e:#}"));
                    continue;
                }
            };
            match decode(root_ty, &contents, res).await {
                Ok(tree) => {
                    let parity = match ev.json.as_ref() {
                        Some(j) => numeric_equal(&tree, &proto_value_to_json(j.as_ref())),
                        None => true,
                    };
                    let bucket = by_type.entry(ty.clone()).or_default();
                    if bucket.len() < samples_per_type {
                        bucket.push(json!({
                            "checkpoint": *seq,
                            "tx_digest": ev.transaction_digest.clone().unwrap_or_default(),
                            "event_index": ev.event_index.unwrap_or_default(),
                            "parity_ok": parity,
                            "decoded": tree,
                        }));
                    }
                }
                Err(e) => failures.push(format!("{ty} @cp{seq}: {e:#}")),
            }
        }
    }

    println!("{} distinct type(s):", by_type.len());
    for (ty, samples) in &by_type {
        println!();
        println!(">> {ty}");
        for s in samples {
            println!("   {}", serde_json::to_string(s)?);
        }
    }
    if !failures.is_empty() {
        println!();
        println!("{} decode failure(s):", failures.len());
        for f in failures.iter().take(10) {
            println!("  ! {f}");
        }
    }
    Ok(())
}

/// Capture live chain-recorded inputs into a replay fixture file
/// (raw BCS + server JSON + on-chain layout), so the decoder can be proven
/// deterministically without network access. See `adapt/replay.rs`.
fn collect_sig_datatypes(body: &sui_rpc::proto::sui::rpc::v2::OpenSignatureBody, out: &mut Vec<String>) {
    use sui_rpc::proto::sui::rpc::v2::open_signature_body::Type;
    if body.r#type.and_then(|t| Type::try_from(t).ok()) == Some(Type::Datatype) {
        if let Some(name) = body.type_name.as_ref() {
            if let Ok(tag) = type_tag::parse(name) {
                out.push(tag.plain());
            }
        }
    }
    for child in &body.type_parameter_instantiation {
        collect_sig_datatypes(child, out);
    }
}

async fn push_fixture_event(
    res: &PackageResolver,
    layouts: &mut std::collections::BTreeMap<String, String>,
    per_type: &mut std::collections::BTreeMap<String, usize>,
    pkg_prefix: &str,
    sub: &str,
    samples_per_type: usize,
    seq: u64,
    ev: &Event,
    events: &mut Vec<Value>,
) -> Result<()> {
    let Some(ty) = ev.event_type.clone() else { return Ok(()) };
    if !ty.starts_with(pkg_prefix) || !ty.contains(sub) {
        return Ok(());
    }
    if per_type.get(&ty).copied().unwrap_or(0) >= samples_per_type {
        return Ok(());
    }
    *per_type.entry(ty.clone()).or_insert(0) += 1;

    let tag = type_tag::parse(&ty)?;
    // Recursively record every layout this type needs (its own and every
    // referenced datatype) so replay is fully network-free.
    let mut pending = vec![tag.plain()];
    let mut visited: std::collections::BTreeSet<String> = Default::default();
    while let Some(key) = pending.pop() {
        if !visited.insert(key.clone()) {
            continue;
        }
        let d = type_tag::parse(&key)?;
        let desc = res.datatype(&d).await.context("fetch layout")?;
        layouts.entry(key.clone()).or_insert_with(|| {
            prost::Message::encode_to_vec(desc.as_ref())
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect()
        });
        for f in &desc.fields {
            if let Some(sig) = f.r#type.as_ref() {
                collect_sig_datatypes(sig, &mut pending);
            }
        }
    }

    let contents = ev
        .contents
        .as_ref()
        .and_then(|b| b.value.as_ref())
        .cloned()
        .map(|b| b.to_vec())
        .unwrap_or_default();
    events.push(json!({
        "event_type": ty,
        "checkpoint": seq,
        "tx_digest": ev.transaction_digest.clone().unwrap_or_default(),
        "event_index": ev.event_index.unwrap_or_default(),
        "contents_hex": contents.iter().map(|b| format!("{b:02x}")).collect::<String>(),
        "server_json": ev.json.as_ref().map(|j| proto_value_to_json(j.as_ref())).unwrap_or(Value::Null),
    }));
    Ok(())
}

fn write_fixture(outfile: &str, comment: String, layouts: &std::collections::BTreeMap<String, String>, events: &[Value]) -> Result<()> {
    if events.is_empty() {
        anyhow::bail!("no fixture events captured");
    }
    let fixture = json!({
        "comment": comment,
        "layouts": layouts,
        "events": events,
    });
    let text = serde_json::to_string_pretty(&fixture)?;
    std::fs::create_dir_all(
        std::path::Path::new(outfile).parent().unwrap_or_else(|| std::path::Path::new(".")),
    )?;
    std::fs::write(outfile, text)?;
    println!(
        "wrote {} events / {} layouts -> {outfile}",
        events.len(),
        layouts.len(),
    );
    Ok(())
}

async fn cmd_fixtures(
    res: &PackageResolver,
    pkg_prefix: &str,
    sub: &str,
    n: u32,
    samples_per_type: usize,
    outfile: &str,
) -> Result<()> {
    let url = env::var("DATABASE_URL").context("DATABASE_URL must be set (see .env.example)")?;
    let pool = sqlx::postgres::PgPoolOptions::new()
        .connect(&url)
        .await
        .context("connect postgres")?;
    let rows: Vec<(i64, Vec<u8>)> =
        sqlx::query_as("select seq, data from raw_checkpoints order by seq desc limit $1")
            .bind(n as i64)
            .fetch_all(&pool)
            .await
            .context("query raw_checkpoints")?;

    let mut layouts: std::collections::BTreeMap<String, String> = Default::default();
    let mut events: Vec<Value> = Vec::new();
    let mut per_type: std::collections::BTreeMap<String, usize> = Default::default();

    for (seq, data) in &rows {
        let cp = capture::decode_checkpoint(data).context("decode stored checkpoint")?;
        for ev in capture::events_in(&cp) {
            push_fixture_event(
                res, &mut layouts, &mut per_type, pkg_prefix, sub, samples_per_type,
                *seq as u64, &ev, &mut events,
            )
            .await?;
        }
    }

    write_fixture(
        outfile,
        format!(
            "Recorded on mainnet via neko-verify fixtures {pkg_prefix} {sub} ({} raw checkpoints up to {}). Not for editing by hand.",
            rows.len(),
            rows.first().map(|(s, _)| *s).unwrap_or(0),
        ),
        &layouts,
        &events,
    )
}

async fn cmd_backfill_fixtures(
    res: &PackageResolver,
    client: &neko_indexer::rpc::Client,
    start: u64,
    end: Option<u64>,
    pkg_prefix: &str,
    sub: &str,
    mod_filter: Option<&[&str]>,
    samples_per_type: usize,
    outfile: &str,
) -> Result<()> {
    let mut req = ListCheckpointsRequest::default();
    req.read_mask = Some(prost_types::FieldMask {
        paths: vec![
            "sequence_number".into(),
            "digest".into(),
            "summary".into(),
            "signature".into(),
            "contents".into(),
            "transactions".into(),
            "objects".into(),
        ],
    });
    req.start_checkpoint = Some(start);
    req.end_checkpoint = end;
    req.filter = mod_filter.map(module_filter);

    let mut layouts: std::collections::BTreeMap<String, String> = Default::default();
    let mut events: Vec<Value> = Vec::new();
    let mut per_type: std::collections::BTreeMap<String, usize> = Default::default();

    let mut stream = Box::pin(client.list_checkpoints(req));
    let mut frames = 0u64;
    while let Some(frame) = stream.next().await {
        let frame = frame.context("list_checkpoints frame")?;
        let Some(cp) = frame.checkpoint else { continue };
        frames += 1;
        if frames % 1000 == 0 {
            println!("  ... {frames} checkpoints scanned");
        }
        for ev in capture::events_in(&cp) {
            push_fixture_event(
                res, &mut layouts, &mut per_type, pkg_prefix, sub, samples_per_type,
                cp.sequence_number.unwrap_or(0), &ev, &mut events,
            )
            .await?;
        }
    }

    write_fixture(
        outfile,
        format!(
            "Recorded on mainnet via neko-verify backfill-fixtures {start}..{range} {pkg_prefix} {sub} ({frames} checkpoints scanned). Not for editing by hand.",
            range = end.map(|e| e.to_string()).unwrap_or_else(|| "tip".into()),
        ),
        &layouts,
        &events,
    )
}

async fn cmd_cetus_swaps(res: &PackageResolver, samples: usize) -> Result<()> {
    println!();
    println!("###### §6 PROCEDURE — Cetus CLMM ({}) ######", short_id(CETUS_CLMM));

    cmd_package(res, CETUS_CLMM).await?;

    println!();
    println!("-- event capture: raw checkpoints (ListEvents not served on public endpoint)");
    cmd_db_scan(res, CETUS_CLMM_DEFINE, "Swap", 300, samples).await
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() {
        eprintln!(
            "usage: neko-verify <package|event-types|datatype|decode|db-scan|cetus-swaps> [args...]"
        );
        std::process::exit(2);
    }

    let c = client().await?;
    let res = PackageResolver::new(c.clone());

    match args[0].as_str() {
        "package" => {
            let id = args.get(1).context("package <id>")?;
            cmd_package(&res, id).await?;
        }
        "event-types" => {
            let module = args.get(1).context("event-types <0xaddr::module>")?;
            let limit = args.get(2).and_then(|v| v.parse().ok()).unwrap_or(200);
            let range = match (args.get(3), args.get(4)) {
                (Some(a), b) => Some((
                    a.parse().context("start seq")?,
                    b.context("end seq")?.parse().context("end seq")?,
                )),
                _ => None,
            };
            cmd_event_types(&c, module, limit, range).await?;
        }
        "datatype" => {
            let id = args.get(1).context("datatype <pkg> <module> <name>")?;
            let module = args.get(2).context("datatype <pkg> <module> <name>")?;
            let name = args.get(3).context("datatype <pkg> <module> <name>")?;
            cmd_datatype(&res, id, module, name).await?;
        }
        "decode" => {
            let ty = args.get(1).context("decode <0xaddr::mod::Event[<T>]>")?;
            let limit = args.get(2).and_then(|v| v.parse().ok()).unwrap_or(5);
            let decoded = cmd_decode(&c, &res, ty, limit).await?;
            println!("{}", serde_json::to_string_pretty(&decoded)?);
        }
        "db-scan" => {
            let pkg = args.get(1).context("db-scan <pkg-prefix> <type-substring> [n]")?;
            let sub = args.get(2).context("db-scan <pkg-prefix> <type-substring> [n]")?;
            let n = args.get(3).and_then(|v| v.parse().ok()).unwrap_or(300);
            let samples = args.get(4).and_then(|v| v.parse().ok()).unwrap_or(5);
            cmd_db_scan(&res, pkg, sub, n, samples).await?;
        }
        "backfill-scan" => {
            let start: u64 = args.get(1).context("backfill-scan <start-seq> [end-seq] <type-prefix> [sub] [max]")?.parse()?;
            let rest = args.iter().skip(2).collect::<Vec<_>>();
            let mut end: Option<u64> = None;
            let mut prefix: Option<&str> = None;
            let mut sub = "";
            let mut max_frames: Option<u64> = None;
            let mut mod_filter: Vec<&str> = Vec::new();
            for arg in rest {
                let numeric = !arg.is_empty() && arg.chars().all(|c| c.is_ascii_digit());
                if let Some(m) = arg.strip_prefix("mod:") {
                    mod_filter.push(m);
                } else if numeric && prefix.is_none() {
                    end = Some(arg.parse()?);
                } else if prefix.is_none() {
                    prefix = Some(arg);
                } else if numeric {
                    max_frames = Some(arg.parse()?);
                } else {
                    sub = arg;
                }
            }
            let prefix = prefix.context("backfill-scan <start-seq> [end-seq] <type-prefix> [sub] [max]")?;
            let mod_filter = (!mod_filter.is_empty()).then_some(mod_filter);
            cmd_backfill_scan(&c, start, end, prefix, sub, mod_filter.as_deref(), max_frames).await?;
        }
        "backfill-fixtures" => {
            let start: u64 = args.get(1).context("backfill-fixtures <start-seq> [end-seq] <type-prefix> [mod:...] [outfile]")?.parse()?;
            let rest = args.iter().skip(2).collect::<Vec<_>>();
            let mut end: Option<u64> = None;
            let mut prefix: Option<&str> = None;
            let mut mod_filter: Vec<&str> = Vec::new();
            let mut out = "tests/fixtures/generic/backfill.json".to_string();
            for arg in rest {
                let numeric = !arg.is_empty() && arg.chars().all(|c| c.is_ascii_digit());
                if let Some(m) = arg.strip_prefix("mod:") {
                    mod_filter.push(m);
                } else if numeric && prefix.is_none() {
                    end = Some(arg.parse()?);
                } else if prefix.is_none() {
                    prefix = Some(arg);
                } else if !numeric {
                    out = arg.to_string();
                }
            }
            let prefix = prefix.context("backfill-fixtures <start-seq> [end-seq] <type-prefix> [mod:...] [outfile]")?;
            let mod_filter = (!mod_filter.is_empty()).then_some(mod_filter);
            cmd_backfill_fixtures(&res, &c, start, end, prefix, "", mod_filter.as_deref(), 8, &out).await?;
        }
        "fixtures" => {
            let pkg = args.get(1).context("fixtures <pkg-prefix> <type-substring> [n] [samples] [outfile]")?;
            let sub = args.get(2).context("fixtures <pkg-prefix> <type-substring> [n] [samples] [outfile]")?;
            let n = args.get(3).and_then(|v| v.parse().ok()).unwrap_or(300);
            let samples = args.get(4).and_then(|v| v.parse().ok()).unwrap_or(6);
            let out = args.get(5).cloned().unwrap_or_else(|| {
                let tag = format!("{sub}");
                let name = tag.chars().filter(|c| c.is_ascii_alphanumeric()).collect::<String>().to_lowercase();
                format!("tests/fixtures/generic/{name}.json")
            });
            cmd_fixtures(&res, pkg, sub, n, samples, &out).await?;
        }
        "cetus-swaps" => {
            let samples = args.get(1).and_then(|v| v.parse().ok()).unwrap_or(5);
            cmd_cetus_swaps(&res, samples).await?;
        }
        "cetus-fixtures" => {
            cmd_fixtures(&res, CETUS_CLMM_DEFINE, "Swap", 300, 6, "tests/fixtures/cetus/swaps.json").await?;
        }
        other => anyhow::bail!("unknown command {other}"),
    }
    Ok(())
}