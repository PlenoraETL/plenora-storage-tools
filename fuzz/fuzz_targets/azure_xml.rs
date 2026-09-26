#![no_main]
libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    plenora_storage_providers::parser_fuzz::azure(data);
});
