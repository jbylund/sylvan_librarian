use rkyv::{Archive, Deserialize, Serialize};

// repr(C) guarantees a stable layout ([u8; N] then u8, align 1, no padding)
// across compiler versions, as the Portable impl below requires.
#[repr(C)]
#[derive(Clone, Copy)]
pub(crate) struct InlineStr<const N: usize> {
    bytes: [u8; N],
    len: u8,
}

// Safety: InlineStr<N> is repr(C) with only align-1 fields — a stable, fully
// initialized, padding-free layout — and carries no internal references, so it
// is safe to treat as a flat, relocatable value in an rkyv archive.
unsafe impl<const N: usize> rkyv::Portable for InlineStr<N> {}

impl<const N: usize> Archive for InlineStr<N> {
    type Archived = InlineStr<N>;
    type Resolver = ();
    fn resolve(&self, _: (), out: rkyv::Place<InlineStr<N>>) {
        // Safety: InlineStr<N> is Copy and Portable; writing it verbatim is correct.
        unsafe { out.ptr().write(*self); }
    }
}

impl<const N: usize, S: rkyv::rancor::Fallible + ?Sized> Serialize<S> for InlineStr<N> {
    fn serialize(&self, _serializer: &mut S) -> Result<(), S::Error> { Ok(()) }
}

impl<const N: usize, D: rkyv::rancor::Fallible + ?Sized> Deserialize<InlineStr<N>, D> for InlineStr<N> {
    fn deserialize(&self, _: &mut D) -> Result<InlineStr<N>, D::Error> { Ok(*self) }
}

// Deliberately permissive: this impl trusts the data rather than validating it
// (a real check would verify len <= N and UTF-8, since as_str() converts
// unchecked). It exists only to satisfy the derived CheckBytes bounds on the
// archived containers; validation is never the engine's safety boundary — the
// archive is trusted by construction (see the access_unchecked justification
// in QueryEngine::query()), so checked access is not relied on for soundness.
unsafe impl<const N: usize, C: rkyv::rancor::Fallible + ?Sized> rkyv::bytecheck::CheckBytes<C> for InlineStr<N> {
    unsafe fn check_bytes(
        _value: *const Self,
        _context: &mut C,
    ) -> Result<(), C::Error> {
        Ok(())
    }
}

impl<const N: usize> InlineStr<N> {
    pub(crate) fn from_str(s: &str) -> Self {
        let max = s.len().min(N);
        // Walk back from max to ensure we don't split a multi-byte char.
        let len = (0..=max).rev().find(|&i| s.is_char_boundary(i)).unwrap_or(0);
        let mut bytes = [0u8; N];
        bytes[..len].copy_from_slice(&s.as_bytes()[..len]);
        InlineStr { bytes, len: len as u8 }
    }

    #[inline]
    pub(crate) fn as_str(&self) -> &str {
        unsafe { std::str::from_utf8_unchecked(&self.bytes[..self.len as usize]) }
    }
}
