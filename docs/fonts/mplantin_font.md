# MPlantin Font Integration

This document describes how the MPlantin font is used for displaying oracle text in the Scryfall OS application.

## Overview

MPlantin (Plantin MT) is the font used on physical Magic: The Gathering cards for **oracle text**.
By using this font in the application, we make the oracle text display authentic and visually similar to physical Magic cards.

The original MPlantin OTF font is ~105KB.
By subsetting it to include only Latin characters and common punctuation, we reduce the file size to ~33KB (WOFF2 format), a 68.7% reduction.

## Implementation

The MPlantin font subsetting follows the same pattern as the Mana and Beleren fonts:

1. **Font Source**: MPlantin OTF from `fonts/mplantin.otf`
1. **License**: Commercial font (not freely redistributable, but we host our own copy)
1. **Subsetting**: Latin characters (U+0020-017F) + smart quotes and punctuation
1. **Formats**: WOFF2 (primary) and WOFF (fallback)
1. **Delivery**: CloudFront CDN with 1-year cache headers
1. **Loading Strategy**: `font-display: swap` to prevent FOIT (Flash of Invisible Text)

## Where It's Used

The MPlantin font is applied to oracle text CSS classes:

- `.card-text` - Oracle text in search results
- `.modal-card-text` - Oracle text in the modal view

These elements use: `font-family: 'MPlantin', Georgia, serif;`

The Georgia fallback ensures text displays properly even if the MPlantin font fails to load.

## Generating and Uploading the Font

### Prerequisites

Install the required Python packages:

```bash
pip install fonttools brotli boto3
```

Configure AWS credentials (if uploading to S3):

```bash
aws configure
```

### Generate Font Files

**Option A: Auto-upload to S3/CloudFront (Recommended)**

```bash
make mplantin_font S3_BUCKET=your-bucket-name
```

Or directly:

```bash
python scripts/subset_mplantin_font.py \
  --input-font fonts/mplantin.otf \
  --output-dir data/fonts/mplantin \
  --cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mplantin \
  --s3-bucket your-bucket-name \
  --s3-prefix cdn/fonts/mplantin
```

This will:

- Take the MPlantin OTF font from `fonts/mplantin.otf`
- Subset it to include only the glyphs we use
- Generate both WOFF2 and WOFF formats (for browser compatibility)
- Create an optimized CSS file with `font-display: swap`
- **Upload all files to S3 with proper cache headers**

**Option B: Generate locally only**

```bash
make mplantin_font
```

Or:

```bash
python scripts/subset_mplantin_font.py \
  --input-font fonts/mplantin.otf \
  --output-dir data/fonts/mplantin \
  --cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mplantin \
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
s3://your-bucket/cdn/fonts/mplantin/mplantin-subset.woff2
s3://your-bucket/cdn/fonts/mplantin/mplantin-subset.woff
s3://your-bucket/cdn/fonts/mplantin/mplantin-subset.css
```

**CORS Configuration**: The script sets up CORS rules to allow font loading from any origin.
This is essential for fonts served from CloudFront/S3.

## HTML Integration

The font is loaded in `api/index.html`:

```html
<link
  href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mplantin/mplantin-subset.css"
  rel="stylesheet"
  type="text/css"
  media="print"
  onload="this.media='all'"
/>
<noscript>
  <link
    href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mplantin/mplantin-subset.css"
    rel="stylesheet"
    type="text/css"
  />
</noscript>
```

The `media="print"` trick ensures the CSS loads asynchronously without blocking page rendering.

## Benefits

After implementing the MPlantin font:

- ✅ Authentic Magic card oracle text appearance
- ✅ Oracle text matches physical cards
- ✅ Optimized file size (68.7% smaller than original)
- ✅ Fast loading with `font-display: swap`
- ✅ Proper fallback to Georgia if font fails to load
- ✅ CDN delivery with long-term caching
- ✅ Same infrastructure as Mana and Beleren fonts

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

1. Oracle text displays in MPlantin font on card search results
1. Oracle text displays in MPlantin font in the card modal
1. Network tab shows `mplantin-subset.woff2` loading from CloudFront
1. Font size is ~33KB (WOFF2) or ~41KB (WOFF)
1. Lighthouse shows good performance score
1. No FOIT (Flash of Invisible Text) occurs
1. Font looks serif/traditional and matches Magic card oracle text style

## Font Comparison

| Feature           | Beleren Font               | MPlantin Font                    |
| ----------------- | -------------------------- | -------------------------------- |
| **Purpose**       | Card titles, type lines    | Oracle text                      |
| **Original Size** | ~58KB (WOFF)               | ~105KB (OTF)                     |
| **Subset Size**   | ~25KB (WOFF2)              | ~33KB (WOFF2)                    |
| **Reduction**     | 56.7%                      | 68.7%                            |
| **Usage**         | `.card-name`, `.card-type` | `.card-text`, `.modal-card-text` |
| **Style**         | Bold sans-serif            | Regular serif                    |
| **Source**        | npm package                | Local OTF file                   |

## Troubleshooting

**Font not loading**: Check the browser console for CORS errors.
Ensure the S3 bucket has proper CORS configuration.

**Oracle text in wrong font**: Check that the CSS classes `.card-text` and `.modal-card-text` have `font-family: 'MPlantin', Georgia, serif;` applied.

**Font looks wrong**: Ensure the MPlantin font is loading from CDN.
Check Network tab in DevTools.

**Large file size**: Verify the subsetting worked.
The WOFF2 file should be ~33KB, not 105KB.

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

If the source MPlantin OTF file needs to be updated:

1. Replace `fonts/mplantin.otf` with the new version
1. Re-run the subsetting script: `make mplantin_font S3_BUCKET=your-bucket-name`
1. Upload the new files to CloudFront
1. Update version in filenames if needed to bust caches
1. Update this documentation

## License Note

MPlantin (Plantin MT) is a commercial font by Monotype.
The font file in this repository (`fonts/mplantin.otf`) is used under license for this project.
The subsetting and web delivery infrastructure is our own implementation.
