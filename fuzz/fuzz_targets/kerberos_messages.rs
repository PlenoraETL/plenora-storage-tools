#![no_main]
libfuzzer_sys::fuzz_target!(|data: &[u8]| {
    smb2::fuzzing::fuzz_kerberos_messages(data);
});
