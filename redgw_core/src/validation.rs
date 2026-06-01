//! 값 크기 검증 — Python validation.py의 Rust 구현
//!
//! PyO3에서 Python str을 &str로 받으면 이미 UTF-8이므로
//! .len()이 바이트 길이를 반환한다 (Python의 .encode() + len() 대체).

use pyo3::prelude::*;

/// UTF-8 바이트 길이가 max_size 이하인지 확인
/// Python의 `len(value.encode()) <= max_size` 대체 — bytes 객체 할당 없음
#[pyfunction]
pub fn check_utf8_byte_len(value: &str, max_size: usize) -> bool {
    value.len() <= max_size
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ascii() {
        assert!(check_utf8_byte_len("hello", 5));
        assert!(check_utf8_byte_len("hello", 10));
        assert!(!check_utf8_byte_len("hello", 4));
    }

    #[test]
    fn test_utf8_multibyte() {
        // "한" = 3 bytes in UTF-8
        assert!(check_utf8_byte_len("한", 3));
        assert!(!check_utf8_byte_len("한", 2));
        // "한글" = 6 bytes
        assert!(check_utf8_byte_len("한글", 6));
        assert!(!check_utf8_byte_len("한글", 5));
    }

    #[test]
    fn test_empty() {
        assert!(check_utf8_byte_len("", 0));
        assert!(check_utf8_byte_len("", 1));
    }
}
