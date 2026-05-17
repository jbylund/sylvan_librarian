# Beleren Font Integration

This document describes how the Beleren font is used for displaying card titles and type lines in the Scryfall OS application, matching the authentic Magic: The Gathering card typography.

## Overview

The Beleren font is the official Magic: The Gathering typeface used on actual cards for **card names and type lines**.
By using this font in the application, we make the card display more authentic and visually similar to physical Magic cards.

The full Beleren Bold font is ~58KB.
By subsetting it to include only Latin characters and common punctuation (the characters needed for English card text), we reduce the file size to ~25KB (WOFF2 format), a 56.7% reduction.

## Implementation

The Beleren font subsetting follows the same pattern as the Mana font:

1. **Font Source**: Beleren Bold from [@saeris/typeface-beleren-bold](https://github.com/Saeris/typeface-beleren-bold) npm package
1. **Version**: 1.0.1
1. **License**: MIT
1. **Subsetting**: Latin characters (U+0020-017F) + smart quotes and punctuation
1. **Formats**: WOFF2 (primary) and WOFF (fallback)
1. **Delivery**: CloudFront CDN with 1-year cache headers
1. **Loading Strategy**: `font-display: swap` to prevent FOIT (Flash of Invisible Text)

## Typography Matching Physical MTG Cards

Following the authentic Magic card typography:

- **Beleren Bold** is used for:
  - `.card-name` - Card titles in search results
  - `.modal-card-name` - Card titles in the modal view
  - `.card-type` - Card type line in search results
  - `.modal-card-type` - Card type line in the modal view

- **MPlantin** (see `docs/mplantin_font.md`) is used for:
  - `.card-text` - Oracle text in search results
  - `.modal-card-text` - Oracle text in the modal view

  Oracle text uses `font-family: 'MPlantin', Georgia, serif;` which loads from CloudFront. The MPlantin font is subsetted from `fonts/mplantin.otf` and hosted on the CDN. If the CDN font fails to load, it falls back to Georgia (a similar serif font).

## Generating and Uploading the Font

### Prerequisites

Install the required Python packages:

```bash
pip install fonttools brotli requests boto3
```

Configure AWS credentials (if uploading to S3):

```bash
aws configure
```

### Generate Font Files

**Option A: Auto-upload to S3/CloudFront (Recommended)**

```bash
make beleren_font S3_BUCKET=your-bucket-name
```

Or directly:

```bash
python scripts/subset_beleren_font.py \
  --output-dir data/fonts/beleren \
  --cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren \
  --s3-bucket your-bucket-name \
  --s3-prefix cdn/fonts/beleren
```

**Option B: Generate locally only**

```bash
make beleren_font
```

Or:

```bash
python scripts/subset_beleren_font.py \
  --output-dir data/fonts/beleren \
  --cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren \
  --skip-upload
```

Then manually upload to S3.

### Verify Upload

The script automatically:

- Configures **CORS** on the S3 bucket (required for font loading!)
- Uploads with proper headers:
  - `Cache-Control: public, max-age=31536000, immutable` (1 year)
  - Correct `Content-Type` for each file format

**Note**: The bucket must allow public read access via a bucket policy (not ACLs)

Files are uploaded to:

```
s3://your-bucket/cdn/fonts/beleren/beleren-subset.woff2
s3://your-bucket/cdn/fonts/beleren/beleren-subset.woff
s3://your-bucket/cdn/fonts/beleren/beleren-subset.css
```

**CORS Configuration**: The script sets up CORS rules to allow font loading from any origin.
This is essential for fonts served from CloudFront/S3.

## HTML Integration

The font is loaded in `api/index.html`:

```html
<link
  href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren/beleren-subset.css"
  rel="stylesheet"
  type="text/css"
  media="print"
  onload="this.media='all'"
/>
<noscript>
  <link
    href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren/beleren-subset.css"
    rel="stylesheet"
    type="text/css"
  />
</noscript>
```

The `media="print"` trick ensures the CSS loads asynchronously without blocking page rendering.

## Benefits

After implementing the Beleren font:

- ✅ More authentic Magic card appearance
- ✅ Oracle text matches physical cards
- ✅ Optimized file size (56.7% smaller than original)
- ✅ Fast loading with `font-display: swap`
- ✅ Proper fallback to sans-serif if font fails to load
- ✅ CDN delivery with long-term caching
- ✅ Same infrastructure as Mana font

## Subsetting Details

The font includes these Unicode ranges:

- **U+0020-007F**: Basic Latin (space through tilde)
- **U+00A0-00FF**: Latin-1 Supplement
- **U+0100-017F**: Latin Extended-A
- **U+2018-201F**: Smart quotes
- **U+2013-2014**: En dash, em dash
- **U+2026**: Ellipsis

This covers all characters needed for English card text, including:

- Letters: A-Z, a-z
- Numbers: 0-9
- Punctuation: .,;:!?'"()-
- Special characters: À, É, Ñ, ü, etc.
- Typographic symbols: —, –, ', ', ", ", …

## Testing

After deployment, verify:

1. Oracle text displays in Beleren font on card search results
1. Oracle text displays in Beleren font in the card modal
1. Network tab shows `beleren-subset.woff2` loading from CloudFront
1. Font size is ~25KB (WOFF2) or ~37KB (WOFF)
1. Lighthouse shows good performance score
1. No FOIT (Flash of Invisible Text) occurs
1. Font looks bold and matches Magic card oracle text style

## Comparison with Mana Font

| Feature           | Mana Font            | Beleren Font                  |
| ----------------- | -------------------- | ----------------------------- |
| **Purpose**       | Mana symbols         | Oracle text                   |
| **Original Size** | ~200-300KB           | ~58KB                         |
| **Subset Size**   | ~20-40KB             | ~25KB                         |
| **Subsetting**    | 64 specific glyphs   | Latin characters              |
| **Usage**         | `.mana-symbol` spans | `.card-text` and `.card-type` |
| **Font Format**   | Icon font            | Text font                     |

## Troubleshooting

**Font not loading**: Check the browser console for CORS errors.
Ensure the S3 bucket has proper CORS configuration.

**Oracle text in wrong font**: Check that the CSS classes `.card-text` and `.modal-card-text` have `font-family: 'Beleren', sans-serif;` applied.

**Font looks thin**: Ensure you're using Beleren **Bold** (font-weight: bold).
The CSS should specify `font-weight: bold`.

**Large file size**: Verify the subsetting worked.
The WOFF2 file should be ~25KB, not 58KB.

**403 Forbidden**: Ensure your S3 bucket policy allows public read access:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::your-bucket/*"
    }
  ]
}
```

## Updating the Font

To update to a newer version of Beleren:

1. Update `BELEREN_FONT_VERSION` in `scripts/subset_beleren_font.py`
1. Re-run the subsetting script
1. Upload the new files to CloudFront
1. Update version in filenames if needed to bust caches
1. Update this documentation

## License

The Beleren font is licensed under the MIT License by Drake Costa.

Copyright (c) 2018 Drake Costa

See: https://github.com/Saeris/typeface-beleren-bold/blob/master/LICENSE.md
