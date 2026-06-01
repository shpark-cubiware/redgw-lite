//! 네임스페이스:타입접두어:키 조립 유틸리티 — Python key_builder.py의 Rust 구현
//!
//! 모든 검증 함수는 실패 시 ValueError를 발생시킨다 (bool 반환 아님).
//! Python wrapper가 이를 HTTPException으로 변환한다.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// namespace 바이트 검증 (regex 대신 수작업 DFA)
/// 허용: A-Z, a-z, 0-9, _, - (1~64자)
#[inline]
fn validate_ns_inner(ns: &str) -> bool {
    let bytes = ns.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes
            .iter()
            .all(|&b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// key 바이트 검증
/// 허용: A-Z, a-z, 0-9, _, ., :, -, / (1~256자)
#[inline]
fn validate_key_inner(key: &str) -> bool {
    let bytes = key.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 256
        && bytes.iter().all(|&b| {
            b.is_ascii_alphanumeric()
                || b == b'_'
                || b == b'.'
                || b == b':'
                || b == b'-'
                || b == b'/'
        })
}

/// 네임스페이스 형식 검증 — 실패 시 ValueError
#[pyfunction]
pub fn validate_ns(ns: &str) -> PyResult<()> {
    if !validate_ns_inner(ns) {
        return Err(PyValueError::new_err(format!(
            "Namespace '{}' contains invalid characters. \
             Allowed: A-Z, a-z, 0-9, _, - (max 64 chars)",
            ns
        )));
    }
    Ok(())
}

/// 키 형식 검증 — 실패 시 ValueError
#[pyfunction]
pub fn validate_key(key: &str) -> PyResult<()> {
    if !validate_key_inner(key) {
        return Err(PyValueError::new_err(format!(
            "Key '{}' contains invalid characters. \
             Allowed: A-Z, a-z, 0-9, _, ., :, -, / (max 256 chars)",
            key
        )));
    }
    Ok(())
}

/// 네임스페이스:타입접두어:키 조립
/// 예: build_key("ERP", "kv", "order-001") → "ERP:kv:order-001"
#[pyfunction]
pub fn build_key(ns: &str, prefix: &str, key: &str) -> PyResult<String> {
    validate_ns(ns)?;
    validate_key(key)?;
    Ok(format!("{}:{}:{}", ns, prefix, key))
}

/// 배치 키 조립 — Python list comprehension 대체
#[pyfunction]
pub fn build_keys_batch(ns: &str, prefix: &str, keys: Vec<String>) -> PyResult<Vec<String>> {
    validate_ns(ns)?;
    let mut result = Vec::with_capacity(keys.len());
    for key in &keys {
        validate_key(key)?;
        result.push(format!("{}:{}:{}", ns, prefix, key));
    }
    Ok(result)
}

/// Redis 키를 (namespace, type_prefix, user_key)로 분리
/// 예: "ERP:kv:order-001" → ("ERP", "kv", "order-001")
#[pyfunction]
pub fn parse_key(redis_key: &str) -> PyResult<(String, String, String)> {
    let first_colon = redis_key.find(':').ok_or_else(|| {
        PyValueError::new_err(format!("Invalid redis key format: {}", redis_key))
    })?;
    let rest = &redis_key[first_colon + 1..];
    let second_colon = rest.find(':').ok_or_else(|| {
        PyValueError::new_err(format!("Invalid redis key format: {}", redis_key))
    })?;
    Ok((
        redis_key[..first_colon].to_string(),
        rest[..second_colon].to_string(),
        rest[second_colon + 1..].to_string(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- validate_ns_inner (내부 함수, PyO3 불필요) ---
    #[test]
    fn test_validate_ns_valid() {
        assert!(validate_ns_inner("ERP"));
        assert!(validate_ns_inner("my_namespace-01"));
        assert!(validate_ns_inner("A"));
        assert!(validate_ns_inner(&"a".repeat(64)));
    }

    #[test]
    fn test_validate_ns_invalid() {
        assert!(!validate_ns_inner(""));
        assert!(!validate_ns_inner(&"a".repeat(65)));
        assert!(!validate_ns_inner("has space"));
        assert!(!validate_ns_inner("has.dot"));
        assert!(!validate_ns_inner("has:colon"));
        assert!(!validate_ns_inner("has/slash"));
    }

    // --- validate_key_inner (내부 함수, PyO3 불필요) ---
    #[test]
    fn test_validate_key_valid() {
        assert!(validate_key_inner("order-001"));
        assert!(validate_key_inner("path/to/resource"));
        assert!(validate_key_inner("ns:sub:key"));
        assert!(validate_key_inner("dotted.key"));
        assert!(validate_key_inner("a"));
        assert!(validate_key_inner(&"k".repeat(256)));
    }

    #[test]
    fn test_validate_key_invalid() {
        assert!(!validate_key_inner(""));
        assert!(!validate_key_inner(&"k".repeat(257)));
        assert!(!validate_key_inner("has space"));
        assert!(!validate_key_inner("한글키"));
    }

    // --- build_key 내부 로직 (validate_ns_inner + validate_key_inner + format) ---
    #[test]
    fn test_build_key_logic_ok() {
        let ns = "ERP";
        let key = "order-001";
        assert!(validate_ns_inner(ns));
        assert!(validate_key_inner(key));
        assert_eq!(format!("{}:{}:{}", ns, "kv", key), "ERP:kv:order-001");
    }

    #[test]
    fn test_build_key_logic_invalid_ns() {
        assert!(!validate_ns_inner(""));
    }

    #[test]
    fn test_build_key_logic_invalid_key() {
        assert!(!validate_key_inner(""));
    }

    // --- parse_key 내부 로직 ---
    #[test]
    fn test_parse_key_logic_ok() {
        let key = "ERP:kv:order-001";
        let first = key.find(':').unwrap();
        let rest = &key[first + 1..];
        let second = rest.find(':').unwrap();
        assert_eq!(&key[..first], "ERP");
        assert_eq!(&rest[..second], "kv");
        assert_eq!(&rest[second + 1..], "order-001");
    }

    #[test]
    fn test_parse_key_with_colons_in_key() {
        let key = "NS:map:a:b:c";
        let first = key.find(':').unwrap();
        let rest = &key[first + 1..];
        let second = rest.find(':').unwrap();
        assert_eq!(&key[..first], "NS");
        assert_eq!(&rest[..second], "map");
        assert_eq!(&rest[second + 1..], "a:b:c");
    }

    #[test]
    fn test_parse_key_invalid() {
        assert!("no-colons".find(':').is_none());
        let key = "one:colon";
        let first = key.find(':').unwrap();
        let rest = &key[first + 1..];
        assert!(rest.find(':').is_none());
    }
}
