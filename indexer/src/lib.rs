//! neko-indexer shared library: Phase 3 on-chain verification + adapter
//! framework, plus the shared gRPC v2 client factory. Used by both the live
//! intake binary (`neko-indexer`) and the §6 verify CLI (`neko-verify`).

pub mod adapt;
pub mod norm;
pub mod rpc;