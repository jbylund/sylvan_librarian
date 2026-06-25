use rkyv::{Archive, Serialize};

/// The cached HTTP response shape stored in shared memory.
/// `headers` is a flat list of pairs rather than a HashMap so rkyv
/// can archive it without needing the hashbrown feature.
#[derive(Archive, Serialize)]
pub struct CachedResponse {
    pub status: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
    pub result_count: Option<i64>,
    pub total_cards: Option<i64>,
}

/// One 64-byte slot in the open-addressing hash table.
#[repr(C)]
pub struct RawSlot {
    pub key_hash: u64,     // EMPTY / TOMBSTONE / normalized xxh3 hash
    pub expiry_ns: u64,    // Unix epoch nanoseconds; u64::MAX = never expires
    pub value_hash: u64,   // xxh3(status || body) — fast-path dedup in set()
    pub arena_offset: u32, // byte offset of rkyv value bytes within the arena
    pub arena_len: u32,    // length of rkyv value bytes
    pub key_offset: u32,   // byte offset of raw key bytes within the arena
    pub key_len: u32,      // length of raw key bytes
    pub body_len: u32,  // raw body byte length — length check before sampled hash
    pub visited: u8, _pad: [u8; 19],
}

const _: () = assert!(std::mem::size_of::<RawSlot>() == 64);
