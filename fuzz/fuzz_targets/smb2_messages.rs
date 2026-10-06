#![no_main]
libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    smb2::fuzzing::fuzz_smb2_messages(data);
});
