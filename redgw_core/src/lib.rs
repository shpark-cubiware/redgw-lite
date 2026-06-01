//! redgw_core — RedGW의 핫패스 성능 최적화를 위한 Rust(PyO3) 모듈
//!
//! 공개 API:
//!   key_builder: validate_ns, validate_key, build_key, build_keys_batch, parse_key
//!   validation:  check_utf8_byte_len

mod key_builder;
mod validation;

use pyo3::prelude::*;

#[pymodule]
fn redgw_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // key_builder
    m.add_function(wrap_pyfunction!(key_builder::validate_ns, m)?)?;
    m.add_function(wrap_pyfunction!(key_builder::validate_key, m)?)?;
    m.add_function(wrap_pyfunction!(key_builder::build_key, m)?)?;
    m.add_function(wrap_pyfunction!(key_builder::build_keys_batch, m)?)?;
    m.add_function(wrap_pyfunction!(key_builder::parse_key, m)?)?;

    // validation
    m.add_function(wrap_pyfunction!(validation::check_utf8_byte_len, m)?)?;



    Ok(())
}
