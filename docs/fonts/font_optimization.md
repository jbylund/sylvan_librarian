# Font Optimization

This document describes how to optimize the Mana font for faster loading and better performance.

## Overview

The Mana font is used to display Magic: The Gathering mana symbols in the application.
The full font is ~200-300KB and contains hundreds of glyphs for set symbols, planeswalker loyalty, and other symbols we don't use.

By subsetting the font to include only the 64 mana symbols we actually use, we can reduce the font size to ~20-40KB, significantly improving:

- Initial page load time
- Lighthouse performance score
- Bandwidth usage for users

## Used Symbols

Our application uses only these symbol types:

- **Basic colors**: W, U, B, R, G, C (6 symbols)
- **Numbers**: 0-16 (17 symbols)
- **Variables**: X, Y, Z (3 symbols)
- **Special**: tap, untap, energy, phyrexian, snow, chaos, pw, infinity (8 symbols)
- **2-color hybrid**: W/U, U/B, B/R, R/G, G/W, W/B, U/R, B/G, R/W, G/U (10 symbols)
- **Generic hybrid**: 2/W, 2/U, 2/B, 2/R, 2/G (5 symbols)
- **Phyrexian hybrid**: W/P, U/P, B/P, R/P, G/P (5 symbols)
- **3-color phyrexian**: W/U/P, W/B/P, U/B/P, U/R/P, B/R/P, B/G/P, R/W/P, R/G/P, G/W/P, G/U/P (10 symbols)

**Total**: 64 unique symbols

## Prerequisites

Install the required Python packages:

```bash
pip install fonttools brotli requests boto3
```

Configure AWS credentials (if uploading to S3):

```bash
aws configure
```

## Steps

### 1. Run the Subsetting Script

**Option A: Auto-upload to S3/CloudFront (Recommended)**

```bash
make fonts S3_BUCKET=your-bucket-name
```

Or directly:

```bash
python scripts/subset_mana_font.py \
  --output-dir data/fonts/mana \
  --cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana \
  --s3-bucket your-bucket-name \
  --s3-prefix cdn/fonts/mana
```

This will:

- Download the original Mana font from GitHub
- Subset it to include only the glyphs we use
- Generate both WOFF2 and WOFF formats (for browser compatibility)
- Create an optimized CSS file with `font-display: swap`
- **Upload all files to S3 with proper cache headers**

**Option B: Generate locally only**

```bash
make fonts
```

Or:

```bash
python scripts/subset_mana_font.py \
  --output-dir data/fonts/mana \
  --cdn-url https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana \
  --skip-upload
```

Then manually upload to S3.

### 2. Verify Upload (if auto-uploaded)

The script automatically:

- Configures **CORS** on the S3 bucket (required for font loading!)
- Uploads with proper headers:
  - `Cache-Control: public, max-age=31536000, immutable` (1 year)
  - Correct `Content-Type` for each file format

**Note**: The bucket must allow public read access via a bucket policy (not ACLs)

Files are uploaded to:

```
s3://your-bucket/cdn/fonts/mana/mana-subset.woff2
s3://your-bucket/cdn/fonts/mana/mana-subset.woff
s3://your-bucket/cdn/fonts/mana/mana-subset.css
```

**CORS Configuration**: The script sets up CORS rules to allow font loading from any origin.
This is essential for fonts served from CloudFront/S3.

### 3. Update index.html

Replace the current jsdelivr CDN link in `api/index.html`:

**Before:**

```html
<link
  href="https://cdn.jsdelivr.net/npm/mana-font@latest/css/mana.min.css"
  rel="stylesheet"
  type="text/css"
  media="print"
  onload="this.media='all'"
/>
```

**After:**

```html
<link
  href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana/mana-subset.css"
  rel="stylesheet"
  type="text/css"
  media="print"
  onload="this.media='all'"
/>
```

Also update the noscript fallback:

```html
<noscript>
  <link href="https://d1hot9ps2xugbc.cloudfront.net/cdn/fonts/mana/mana-subset.css" rel="stylesheet" type="text/css" />
</noscript>
```

### 4. Remove the @font-face Override

Remove the `@font-face` override in the `<style>` section since the CSS file now has the proper `font-display: swap` declaration:

**Remove:**

```css
/* Ensure Mana font displays with swap to prevent invisible text */
@font-face {
  font-family: 'Mana';
  font-display: swap;
}
```

## Benefits

After implementing this optimization, you should see:

- ✅ Reduced font file size (80-90% smaller)
- ✅ Faster initial page load
- ✅ Better Lighthouse performance score
- ✅ `font-display: swap` ensuring text remains visible during font load
- ✅ Same-origin resource loading (no extra DNS lookup)
- ✅ Version stability (no `@latest` dependency)
- ✅ Better caching control

## Updating the Font

If you need to add new mana symbols in the future:

1. Add the symbol classes to the `USED_SYMBOLS` list in `scripts/subset_mana_font.py`
1. Add the corresponding CSS in the `generate_css()` function
1. Re-run the subsetting script
1. Upload the new files to CloudFront
1. Update the version number in filenames if needed to bust caches

## Testing

After deployment, verify:

1. Mana symbols display correctly on the page
1. Network tab shows the subsetted font loading from CloudFront
1. Font size is significantly reduced
1. Lighthouse shows improved performance score
1. No FOIT (Flash of Invisible Text) occurs

## Troubleshooting

**CORS Missing / Font blocked**: This is the most common issue.
The script configures CORS automatically, but if you uploaded manually:

1. Re-run the script with `--s3-bucket` to configure CORS automatically
1. Or manually configure CORS on your S3 bucket:
   - Go to S3 bucket → Permissions → CORS
   - Add this configuration:
   ```json
   [
     {
       "AllowedHeaders": ["*"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedOrigins": ["*"],
       "ExposeHeaders": ["ETag"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```
1. If using CloudFront, ensure it forwards the `Origin` header to S3

**Symbols not displaying**: Check the browser console for font loading errors.
Verify the CDN URL is correct and files are accessible.

**Font looks different**: Ensure you're using the same version of the Mana font.
Update the `MANA_FONT_VERSION` in the script if needed.

**Large file size**: Verify the subsetting actually worked by checking the file sizes.
The WOFF2 file should be under 50KB.

**403 Forbidden**: Ensure your S3 bucket policy allows public read access for the files.
The bucket should have a policy like:

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
