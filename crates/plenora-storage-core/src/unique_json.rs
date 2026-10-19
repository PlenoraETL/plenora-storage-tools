//! Recursive duplicate-member rejection before typed JSON deserialization.
use std::{collections::BTreeSet, fmt};

use serde::{
    Deserialize, Deserializer,
    de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor},
};

use crate::{StorageError, StorageResult};

struct UniqueJson<'a>(&'a mut bool);

impl<'de> DeserializeSeed<'de> for UniqueJson<'_> {
    type Value = ();

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<(), D::Error> {
        deserializer.deserialize_any(self)
    }
}

impl<'de> Visitor<'de> for UniqueJson<'_> {
    type Value = ();

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("JSON with unique object members")
    }

    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<(), M::Error> {
        let mut keys = BTreeSet::new();
        while let Some(key) = map.next_key::<String>()? {
            if !keys.insert(key) {
                *self.0 = true;
                return Err(de::Error::custom(
                    "JSON document contains duplicate members",
                ));
            }
            map.next_value_seed(UniqueJson(self.0))?;
        }
        Ok(())
    }

    fn visit_seq<S: SeqAccess<'de>>(self, mut sequence: S) -> Result<(), S::Error> {
        while sequence.next_element_seed(UniqueJson(self.0))?.is_some() {}
        Ok(())
    }

    fn visit_bool<E: de::Error>(self, _: bool) -> Result<(), E> {
        Ok(())
    }
    fn visit_i64<E: de::Error>(self, _: i64) -> Result<(), E> {
        Ok(())
    }
    fn visit_u64<E: de::Error>(self, _: u64) -> Result<(), E> {
        Ok(())
    }
    fn visit_f64<E: de::Error>(self, _: f64) -> Result<(), E> {
        Ok(())
    }
    fn visit_str<E: de::Error>(self, _: &str) -> Result<(), E> {
        Ok(())
    }
    fn visit_unit<E: de::Error>(self) -> Result<(), E> {
        Ok(())
    }
}

/// Check a complete JSON document before materializing maps or validating schemas.
///
/// # Errors
/// Returns a redacted invalid-configuration error for duplicate members or invalid JSON.
pub fn validate_json(bytes: &[u8]) -> StorageResult<()> {
    let mut duplicate = false;
    let mut parser = serde_json::Deserializer::from_slice(bytes);
    UniqueJson(&mut duplicate)
        .deserialize(&mut parser)
        .and_then(|()| parser.end())
        .map_err(|_| {
            StorageError::invalid_configuration(
                if duplicate {
                    "JSON_DUPLICATE_MEMBER"
                } else {
                    "JSON_INVALID"
                },
                if duplicate {
                    "JSON document contains duplicate members"
                } else {
                    "JSON document is invalid"
                },
            )
        })
}

// Field-level protection also covers consumers calling serde_json directly.
// The visitor constructs a Value only after each member name is checked.
pub fn deserialize<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: de::DeserializeOwned,
{
    let value = UniqueValue::deserialize(deserializer)?;
    T::deserialize(value.0).map_err(de::Error::custom)
}

struct UniqueValue(serde_json::Value);

impl<'de> Deserialize<'de> for UniqueValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(ValueVisitor)
    }
}

struct ValueVisitor;

impl<'de> Visitor<'de> for ValueVisitor {
    type Value = UniqueValue;
    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("JSON with unique object members")
    }
    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
        let mut result = serde_json::Map::new();
        while let Some(key) = map.next_key::<String>()? {
            if result.contains_key(&key) {
                return Err(de::Error::custom(
                    "JSON document contains duplicate members",
                ));
            }
            result.insert(key, map.next_value::<UniqueValue>()?.0);
        }
        Ok(UniqueValue(result.into()))
    }
    fn visit_seq<S: SeqAccess<'de>>(self, mut sequence: S) -> Result<Self::Value, S::Error> {
        let mut result = Vec::new();
        while let Some(value) = sequence.next_element::<UniqueValue>()? {
            result.push(value.0);
        }
        Ok(UniqueValue(result.into()))
    }
    fn visit_bool<E: de::Error>(self, value: bool) -> Result<Self::Value, E> {
        Ok(UniqueValue(value.into()))
    }
    fn visit_i64<E: de::Error>(self, value: i64) -> Result<Self::Value, E> {
        Ok(UniqueValue(value.into()))
    }
    fn visit_u64<E: de::Error>(self, value: u64) -> Result<Self::Value, E> {
        Ok(UniqueValue(value.into()))
    }
    fn visit_f64<E: de::Error>(self, value: f64) -> Result<Self::Value, E> {
        Ok(UniqueValue(value.into()))
    }
    fn visit_str<E: de::Error>(self, value: &str) -> Result<Self::Value, E> {
        Ok(UniqueValue(value.into()))
    }
    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(UniqueValue(serde_json::Value::Null))
    }
}

#[cfg(test)]
#[path = "unique_json_tests.rs"]
mod json_tests;
