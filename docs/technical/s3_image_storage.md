# S3 Image Storage and CloudFront Distribution

## Overview

Sylvan Librarian stores card images in Amazon S3 with a CloudFront distribution in front of it for fast global delivery. Images are processed from Scryfall's PNG format into optimized WebP format at multiple resolutions.

## Architecture

```
Scryfall PNG → Processing Script → S3 Bucket → CloudFront CDN → Application
```

## S3 Storage Structure

### Bucket Configuration
- **Bucket Name**: `biblioplex`
- **Region**: Not specified in code (uses default AWS region)
- **Access**: Public read access for CloudFront

### Image Path Structure
Card images are stored in a face-aware hierarchy to support double-faced and multi-faced cards:

```
s3://biblioplex/img/{set_code}/{collector_number}/{face}/{width}.webp
```

For single-faced cards, `face` is `"1"`. For double-faced cards, faces are numbered `"1"` and `"2"`.

**Example paths:**
- `s3://biblioplex/img/iko/1/1/280.webp` (small, face 1)
- `s3://biblioplex/img/iko/1/1/388.webp` (medium, face 1)
- `s3://biblioplex/img/iko/1/1/538.webp` (large, face 1)
- `s3://biblioplex/img/iko/1/1/745.webp` (extra large, face 1)

### Image Sizes and Quality

| Size Key | Width (px) | Use Case | Quality |
|----------|------------|----------|---------|
| `280` | 280px | Small displays, thumbnails | 85% WebP |
| `388` | 388px | Medium displays, mobile | 85% WebP |
| `538` | 538px | Large displays, tablets | 85% WebP |
| `745` | 745px | Full resolution, desktop | 85% WebP |

## CloudFront Distribution

### Distribution Details
- **Domain**: `d1hot9ps2xugbc.cloudfront.net`
- **Origin**: S3 bucket `biblioplex`
- **Caching**: 20-day cache duration with immutable headers

### Public URLs
Images are accessible via CloudFront at:

```
https://d1hot9ps2xugbc.cloudfront.net/img/{set_code}/{collector_number}/{face}/{width}.webp
```

**Example URLs:**
- `https://d1hot9ps2xugbc.cloudfront.net/img/iko/1/1/280.webp`
- `https://d1hot9ps2xugbc.cloudfront.net/img/iko/1/1/388.webp`
- `https://d1hot9ps2xugbc.cloudfront.net/img/iko/1/1/538.webp`
- `https://d1hot9ps2xugbc.cloudfront.net/img/iko/1/1/745.webp`

## Image Processing Pipeline

### Source Data
- **Source**: Scryfall PNG images (745px width)
- **Database**: PostgreSQL with card metadata
- **Processing**: Python script with multiprocessing

### Processing Steps
1. **Fetch**: Download PNG from Scryfall using `png_url` from database
2. **Convert**: Use `cwebp` tool to convert PNG → WebP
3. **Resize**: Generate 4 sizes (280px, 388px, 538px, 745px)
4. **Upload**: Store in S3 with proper headers

### Technical Details
- **Format**: WebP with 85% quality
- **Resize**: Width-based with auto-calculated height
- **Processing**: Parallel processing with configurable worker count (default: 8)
- **Timeout**: 30-second timeout for downloads and conversions

## S3 Object Configuration

### Cache Headers
```http
Cache-Control: public, max-age=1728000, immutable
Content-Type: image/webp
```

- **Cache Duration**: 20 days (1,728,000 seconds)
- **Immutable**: Objects never change once uploaded
- **Content-Type**: Proper WebP MIME type

### Upload Process
- **Skip Existing**: Default behavior to avoid re-processing
- **Dry Run**: Test mode without actual uploads
- **Error Handling**: Graceful failure with detailed logging

## Usage in Application

### URL Generation
The application generates image URLs using the face-aware structure:

```
https://d1hot9ps2xugbc.cloudfront.net/img/{set_code}/{collector_number}/{face}/{width}.webp
```

The `face` parameter defaults to `"1"` for single-faced cards and is set to `"1"` or `"2"` for double-faced cards based on the card's face data.

## Management Scripts

### Primary Script
- **File**: `scripts/copy_images_to_s3.py`
- **Purpose**: Download, convert, and upload card images
- **Features**:
  - Parallel processing
  - Skip existing images
  - Dry run mode
  - Set-specific filtering
  - Progress tracking

### Usage Examples
```bash
# Process all cards
python scripts/copy_images_to_s3.py

# Process specific set
python scripts/copy_images_to_s3.py --set iko

# Dry run (no actual uploads)
python scripts/copy_images_to_s3.py --dry-run

# Limit number of cards
python scripts/copy_images_to_s3.py --limit 100
```

## Performance Characteristics

### Storage Efficiency
- **Format**: WebP provides ~25-35% size reduction vs PNG
- **Quality**: 85% quality maintains visual fidelity
- **Sizes**: 4 optimized sizes prevent over-downloading

### Delivery Performance
- **CDN**: CloudFront provides global edge caching
- **Cache**: 20-day cache reduces origin requests
- **Headers**: Immutable cache prevents unnecessary revalidation

### Processing Performance
- **Parallel**: 8-worker default for concurrent processing
- **Skip Logic**: Avoids re-processing existing images
- **Progress**: Real-time progress tracking with ETA

## Monitoring and Maintenance

### Health Checks
- **S3 Access**: Verify bucket permissions
- **CloudFront**: Check distribution status
- **Image Quality**: Validate WebP conversion
- **URL Generation**: Test URL construction

### Scaling Considerations
- **Storage**: S3 scales automatically
- **Processing**: Increase worker count for faster processing
- **CDN**: CloudFront handles traffic spikes
- **Costs**: Monitor S3 storage and CloudFront transfer costs

## Security and Access

### S3 Permissions
- **Public Read**: Required for CloudFront access
- **CORS**: Configured for cross-origin requests
- **Encryption**: S3 server-side encryption (if configured)

### CloudFront Security
- **HTTPS**: All requests served over HTTPS
- **Origin**: S3 bucket as origin
- **Headers**: Proper cache and content-type headers

## Troubleshooting

### Common Issues
1. **Missing Images**: Check S3 bucket permissions
2. **Slow Loading**: Verify CloudFront distribution
3. **Conversion Errors**: Ensure `cwebp` is installed
4. **Upload Failures**: Check AWS credentials

### Debug Commands
```bash
# Check S3 access
aws s3 ls s3://biblioplex/img/

# Test CloudFront
curl -I https://d1hot9ps2xugbc.cloudfront.net/img/iko/1/1/280.webp

# Verify cwebp installation
cwebp -version
```

## Future Considerations

### Potential Improvements
- **Format**: Consider AVIF for even better compression
- **Sizes**: Add more size variants for different use cases
- **Processing**: Implement incremental updates
- **Monitoring**: Add CloudWatch metrics and alerts

### Cost Optimization
- **Storage**: Monitor S3 storage costs
- **Transfer**: Track CloudFront data transfer
- **Processing**: Optimize worker count for cost/performance
- **Caching**: Fine-tune cache durations
