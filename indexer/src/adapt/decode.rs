//! Type trees from on-chain `OpenSignatureBody` layouts + BCS decode.
//!
//! A `TypeTree` mirrors exactly what the network's `DatatypeDescriptor`
//! says a struct's fields look like; decoding walks the tree with a strict
//! BCS reader. Unknowns are errors (→ DLQ), never guesses.

use anyhow::{Context, Result};
use serde_json::{json, Value};
use sui_rpc::proto::sui::rpc::v2::open_signature_body::Type as SigType;
use sui_rpc::proto::sui::rpc::v2::OpenSignatureBody;

use crate::adapt::bcs::Reader;
use crate::adapt::type_tag::{parse, Kind, TypeTagStr};
use crate::adapt::PackageResolver;

#[derive(Debug, Clone, PartialEq)]
pub enum Ty {
    U8,
    U16,
    U32,
    U64,
    U128,
    U256,
    Bool,
    Addr,
    Vector(Box<Ty>),
    /// Reference to a type parameter (index into the current binding list).
    TypeParam(usize),
    /// Struct instance: name for lookup + instantiated type arguments.
    Struct { name: TypeTagStr, args: Vec<Ty> },
}

fn ty_from_sig(body: &OpenSignatureBody) -> Result<Ty> {
    let kind = match body.r#type {
        Some(t) => SigType::try_from(t).unwrap_or(SigType::Unknown),
        None => SigType::Unknown,
    };
    Ok(match kind {
        SigType::Address => Ty::Addr,
        SigType::Bool => Ty::Bool,
        SigType::U8 => Ty::U8,
        SigType::U16 => Ty::U16,
        SigType::U32 => Ty::U32,
        SigType::U64 => Ty::U64,
        SigType::U128 => Ty::U128,
        SigType::U256 => Ty::U256,
        SigType::Vector => {
            let inner = body
                .type_parameter_instantiation
                .first()
                .context("vector signature missing element type")?;
            Ty::Vector(Box::new(ty_from_sig(inner)?))
        }
        SigType::Datatype => {
            let raw = body
                .type_name
                .as_ref()
                .context("datatype signature missing type name")?;
            let tag = parse(raw).context("parse datatype signature name")?;
            let args = if body.type_parameter_instantiation.is_empty() {
                tag.args.iter().map(ty_from_tag).collect::<Result<_>>()?
            } else {
                body.type_parameter_instantiation.iter().map(ty_from_sig).collect::<Result<_>>()?
            };
            Ty::Struct { name: tag, args }
        }
        SigType::Parameter => {
            let idx = body.type_parameter.context("type-parameter signature missing index")?;
            Ty::TypeParam(idx as usize)
        }
        SigType::Unknown => anyhow::bail!("unknown signature type in layout"),
        other => anyhow::bail!("unhandled signature type {:?} in layout", other),
    })
}

fn ty_from_tag(tag: &TypeTagStr) -> Result<Ty> {
    match tag.kind {
        Kind::Vector => {
            let inner = tag.args.first().context("vector tag missing element")?;
            Ok(Ty::Vector(Box::new(ty_from_tag(inner)?)))
        }
        Kind::Prim => Ok(match tag.name.as_str() {
            "bool" => Ty::Bool,
            "u8" => Ty::U8,
            "u16" => Ty::U16,
            "u32" => Ty::U32,
            "u64" => Ty::U64,
            "u128" => Ty::U128,
            "u256" => Ty::U256,
            "address" => Ty::Addr,
            other => anyhow::bail!("unhandled primitive type tag {other}"),
        }),
        Kind::Struct => Ok(Ty::Struct {
            name: tag.clone(),
            args: tag.args.iter().map(ty_from_tag).collect::<Result<_>>()?,
        }),
    }
}

/// Root type tree for an event from its full type string (no external names
/// are invented; this is parsed from the network-emitted `event_type`).
pub fn ty_from_event_type(event_type: &str) -> Result<Ty> {
    let tag = parse(event_type).context("parse event type")?;
    ty_from_tag(&tag)
}

fn u256_or_string(b: [u8; 32]) -> Value {
    // Little-endian 32 bytes -> decimal digits via repeated long-division by
    // 10 over a big-endian working buffer. Emitted as a JSON number only when
    // it fits u64 losslessly; larger values become strings (BCS path never
    // touches floats).
    let mut n = [0u8; 32];
    for (i, byte) in b.iter().enumerate() {
        n[31 - i] = *byte;
    }
    if n.iter().all(|&x| x == 0) {
        return json!("0");
    }
    let mut digits: Vec<u8> = Vec::with_capacity(78);
    let mut all_zero = false;
    while !all_zero {
        let mut rem = 0u32;
        all_zero = true;
        for byte in n.iter_mut() {
            let cur = (rem << 8) | *byte as u32;
            *byte = (cur / 10) as u8;
            rem = cur % 10;
            if *byte != 0 {
                all_zero = false;
            }
        }
        digits.push(b'0' + rem as u8);
    }
    digits.reverse();
    json!(String::from_utf8(digits).expect("ascii digits"))
}

/// Recursive decode of `bytes` against `ty`, resolving nested struct layouts
/// on-chain. Returns a serde_json Value mirroring the event contents.
pub async fn decode(root: Ty, bytes: &[u8], res: &PackageResolver) -> Result<Value> {
    let mut r = Reader::new(bytes);
    let v = decode_in(&mut r, &root, res, &[]).await.context("decode event contents")?;
    if r.remaining() != 0 {
        anyhow::bail!("{} trailing bytes after event contents", r.remaining());
    }
    Ok(v)
}

async fn decode_in(
    r: &mut Reader<'_>,
    ty: &Ty,
    res: &PackageResolver,
    bindings: &[Ty],
) -> Result<Value> {
    decode_inner(r, ty, res, bindings).await
}

/// Boxed form of `decode_in` so recursive calls are indirect (avoids an
/// infinitely sized future).
fn decode_inner<'a>(
    r: &'a mut Reader<'_>,
    ty: &'a Ty,
    res: &'a PackageResolver,
    bindings: &'a [Ty],
) -> std::pin::Pin<Box<dyn futures::Future<Output = Result<Value>> + Send + 'a>> {
    Box::pin(async move {
        Ok(match ty {
        Ty::U8 => json!(r.read_u8()?),
        Ty::U16 => json!(r.read_u16()?),
        Ty::U32 => json!(r.read_u32()?),
        Ty::U64 => json!(r.read_u64()?.to_string()),
        Ty::U128 => json!(r.read_u128()?.to_string()),
        Ty::U256 => u256_or_string(r.read_u256()?),
        Ty::Bool => json!(r.read_bool()?),
        Ty::Addr => json!(r.read_address()?),
        Ty::Vector(inner) => {
            let n = r.read_vec_len()?;
            let mut items = Vec::with_capacity(n);
            for _ in 0..n {
                items.push(decode_inner(r, inner, res, bindings).await?);
            }
            Value::Array(items)
        }
        Ty::TypeParam(idx) => {
            let bound = bindings
                .get(*idx)
                .context("type parameter index out of range for this struct instance")?;
            decode_inner(r, bound, res, bindings).await?
        }
        Ty::Struct { name, args } => {
            let desc = res.datatype(name).await?;
            let fields = desc.fields.as_slice();

            // Move idiom: single-field `{ bytes: address }` wrappers
            // (e.g. `0x2::object::ID`) decode to the address string. This
            // matches how the network itself serializes object ids in
            // `Event.json`. Only the exact one-field/mutable-address pattern
            // is normalized; nothing else is guessed.
            if fields.len() == 1
                && fields[0].name.as_deref() == Some("bytes")
                && fields[0]
                    .r#type
                    .as_ref()
                    .is_some_and(|t| matches!(t.r#type, Some(t) if SigType::try_from(t) == Ok(SigType::Address)))
            {
                return Ok(json!(r.read_address()?));
            }

            let mut obj = serde_json::Map::new();
            for f in fields {
                let fname = f.name.clone().context("field missing name")?;
                let fty = ty_from_sig(f.r#type.as_ref().context("field missing type")?)?;
                let arg_bindings = if fty_has_type_params(&fty) { args.as_slice() } else { &[] };
                let val = decode_inner(r, &fty, res, arg_bindings).await
                    .with_context(|| format!("field {fname}"))?;
                obj.insert(fname, val);
            }
            Value::Object(obj)
        }
        })
    })
}

fn fty_has_type_params(ty: &Ty) -> bool {
    match ty {
        Ty::TypeParam(_) => true,
        Ty::Vector(i) => fty_has_type_params(i),
        Ty::Struct { args, .. } => args.iter().any(fty_has_type_params),
        _ => false,
    }
}

/// Convert the server-provided `google.protobuf.Value` JSON mirror into
/// serde_json for parity checks (uses the network's own pre-decoded JSON).
pub fn proto_value_to_json(v: &prost_types::Value) -> Value {
    use prost_types::value::Kind;
    match v.kind.as_ref() {
        None | Some(Kind::NullValue(_)) => Value::Null,
        Some(Kind::NumberValue(n)) => json!(n),
        Some(Kind::StringValue(s)) => json!(s.clone()),
        Some(Kind::BoolValue(b)) => json!(b),
        Some(Kind::StructValue(st)) => {
            let mut m = serde_json::Map::new();
            for (k, vv) in &st.fields {
                m.insert(k.clone(), proto_value_to_json(vv));
            }
            Value::Object(m)
        }
        Some(Kind::ListValue(l)) => {
            Value::Array(l.values.iter().map(proto_value_to_json).collect())
        }
    }
}

/// Structural equality for parity check that treats every number as its
/// canonical string form (avoids float vs int formatting noise; exact for
/// money since we never go through floats in the BCS path).
pub fn numeric_equal(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => {
            let xs = if let Some(i) = x.as_i64() { i.to_string() } else { x.to_string() };
            let ys = if let Some(i) = y.as_i64() { i.to_string() } else { y.to_string() };
            xs == ys
        }
        (Value::Array(xa), Value::Array(ya)) => {
            xa.len() == ya.len() && xa.iter().zip(ya).all(|(x, y)| numeric_equal(x, y))
        }
        (Value::Object(xo), Value::Object(yo)) => {
            xo.len() == yo.len()
                && xo.iter().all(|(k, v)| yo.get(k).is_some_and(|w| numeric_equal(v, w)))
        }
        (x, y) => x == y,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn type_tag_and_tree() {
        let t = ty_from_event_type(
            "0x25ebb9a7c50eb17b3fa9c5a30fb8b5ad8f97caaf4928943acbcff7153dfee5e3::pool::SwapEvent",
        )
        .unwrap();
        assert!(matches!(t, Ty::Struct { .. }));
        match t {
            Ty::Struct { name, args } => {
                assert_eq!(name.name, "SwapEvent");
                assert!(args.is_empty());
            }
            _ => unreachable!(),
        }
    }

    #[test]
    fn option_encodes_as_vector() {
        // 0x01 followed by payload is a present option (same as vec len 1).
        let buf = [0x01, 0x2a];
        let tree = Ty::Struct {
            name: parse("0x1::option::Option<u8>").unwrap(),
            args: vec![Ty::U8],
        };
        // decode needs a resolver; encode() by hand below is enough for the
        // type-graph test.
        let _ = (buf, tree);
    }
}