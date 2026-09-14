//! Phase 3 adapter framework: on-chain layout resolution, strict BCS decode,
//! and the §6 verification procedure (`neko-verify`).
//!
//! Rule of the framework: the network defines both the event names and the
//! field layouts. Nothing here hard-codes a struct shape; every decode starts
//! from a `GetDatatype` descriptor fetched from the chain.

pub mod bcs;
pub mod capture;
pub mod decode;
pub mod normalize;
pub mod replay;
pub mod resolver;
pub mod type_tag;

pub use capture::{decode_checkpoint, events_in};
pub use decode::{decode, proto_value_to_json, ty_from_event_type, numeric_equal};
pub use normalize::{map_trade, Side, TokenTrade, Venue, CETUS_DEFINE, SUIPUMP_DEFINE};
pub use resolver::PackageResolver;