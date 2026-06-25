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
