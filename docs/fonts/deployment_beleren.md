# Beleren Font Deployment Guide

Quick guide to deploy the Beleren font to production.

## Prerequisites

1. AWS credentials configured (`aws configure`)
1. S3 bucket set up with public read access
1. CloudFront distribution pointing to the S3 bucket

## Deployment Steps

### 1. Generate and Upload Font Files

```bash
make beleren_font S3_BUCKET=your-bucket-name
```

This will:

- Download the original Beleren Bold font
- Subset it to Latin characters only (56.7% size reduction)
- Generate WOFF2 and WOFF formats
- Generate optimized CSS file
- Upload all files to S3 with proper CORS headers
- Set cache headers for 1-year caching

### 2. Verify Upload

Files should be available at:

```
https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren/beleren-subset.woff2
https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren/beleren-subset.woff
https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/beleren/beleren-subset.css
```

### 3. Test

1. Open your Scryfall OS instance
1. Search for any card
1. Check that:
   - Card titles display in bold Beleren font
   - Type lines display in bold Beleren font
   - Oracle text displays in serif font (MPlantin/Georgia)
1. Verify in browser DevTools that font loads from CDN
1. Check that font file is ~25KB (WOFF2)

## What Gets Uploaded

| File                   | Size   | Content-Type | Purpose                        |
| ---------------------- | ------ | ------------ | ------------------------------ |
| `beleren-subset.woff2` | ~25KB  | `font/woff2` | Primary font (modern browsers) |
| `beleren-subset.woff`  | ~37KB  | `font/woff`  | Fallback font (older browsers) |
| `beleren-subset.css`   | ~0.5KB | `text/css`   | Font-face declarations         |

## S3 Configuration

The script automatically configures:

1. **CORS Rules** - Allows font loading from any origin
1. **Cache Headers** - `Cache-Control: public, max-age=31536000, immutable`
1. **Content Types** - Proper MIME types for each file

Ensure your S3 bucket has this policy:

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

## Troubleshooting

### Font not loading (CORS error)

Re-run the script to reconfigure CORS:

```bash
make beleren_font S3_BUCKET=your-bucket-name
```

### Font not displaying

1. Check browser console for errors
1. Verify CSS is loaded: View Source → look for `beleren-subset.css` link
1. Verify font files are accessible (visit URLs directly)
1. Check that:
   - `.card-name` and `.card-type` have `font-family: 'Beleren', sans-serif;`
   - `.card-text` has `font-family: 'MPlantin', 'Plantin MT Pro', 'Plantin', Georgia, serif;`

### Wrong font weight

Ensure the CSS specifies `font-weight: bold` for Beleren Bold on card titles and type lines

## Manual Upload (if needed)

If you can't use the automated script:

```bash
# Generate fonts locally
make beleren_font

# Upload manually
aws s3 cp data/fonts/beleren/beleren-subset.woff2 \
  s3://your-bucket/cdn/fonts/beleren/beleren-subset.woff2 \
  --content-type "font/woff2" \
  --cache-control "public, max-age=31536000, immutable"

aws s3 cp data/fonts/beleren/beleren-subset.woff \
  s3://your-bucket/cdn/fonts/beleren/beleren-subset.woff \
  --content-type "font/woff" \
  --cache-control "public, max-age=31536000, immutable"

aws s3 cp data/fonts/beleren/beleren-subset.css \
  s3://your-bucket/cdn/fonts/beleren/beleren-subset.css \
  --content-type "text/css; charset=utf-8" \
  --cache-control "public, max-age=31536000, immutable"
```

Then manually configure CORS on the S3 bucket.

## Rollback

To revert to sans-serif:

1. Remove the Beleren font link from `api/index.html`
1. Remove `font-family: 'Beleren', sans-serif;` from CSS classes
1. Deploy updated `index.html`

The font files can remain on CDN (they won't be loaded if not referenced).

## Performance Impact

- **File Size**: +25KB (WOFF2) on first load, then cached
- **Load Time**: ~50-100ms on first load (from CDN)
- **Subsequent Loads**: 0ms (cached for 1 year)
- **Page Weight**: Minimal increase, significant visual improvement

## Next Steps

After deployment:

1. Monitor CloudFront logs for font file requests
1. Check Lighthouse performance score
1. Gather user feedback on appearance
1. Consider adding Beleren SmallCaps variant if needed
