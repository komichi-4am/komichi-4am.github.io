#import <AppKit/AppKit.h>
#import <CoreImage/CoreImage.h>
#import <Foundation/Foundation.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3) {
            fprintf(stderr, "usage: render_qr VALUE OUTPUT.png\n");
            return 2;
        }

        NSString *value = [NSString stringWithUTF8String:argv[1]];
        NSString *outputPath = [NSString stringWithUTF8String:argv[2]];
        NSData *message = [value dataUsingEncoding:NSUTF8StringEncoding];
        CIFilter *generator = [CIFilter filterWithName:@"CIQRCodeGenerator"];
        if (!message || !generator) {
            fprintf(stderr, "could not initialize QR generator\n");
            return 2;
        }
        [generator setValue:message forKey:@"inputMessage"];
        [generator setValue:@"M" forKey:@"inputCorrectionLevel"];
        CIImage *rawImage = generator.outputImage;
        if (!rawImage) {
            fprintf(stderr, "could not generate QR image\n");
            return 2;
        }

        CGFloat scale = 10.0;
        CGFloat padding = 40.0;
        CIImage *scaled = [rawImage imageByApplyingTransform:CGAffineTransformMakeScale(scale, scale)];
        CIContext *context = [CIContext contextWithOptions:nil];
        CGImageRef qrImage = [context createCGImage:scaled fromRect:scaled.extent];
        if (!qrImage) {
            fprintf(stderr, "could not render QR image\n");
            return 2;
        }
        size_t width = CGImageGetWidth(qrImage) + (size_t)(padding * 2);
        size_t height = CGImageGetHeight(qrImage) + (size_t)(padding * 2);
        CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
        CGContextRef bitmapContext = CGBitmapContextCreate(
            NULL,
            width,
            height,
            8,
            width * 4,
            colorSpace,
            kCGImageAlphaPremultipliedLast
        );
        CGColorSpaceRelease(colorSpace);
        if (!bitmapContext) {
            CGImageRelease(qrImage);
            fprintf(stderr, "could not create QR bitmap\n");
            return 2;
        }
        CGContextSetRGBFillColor(bitmapContext, 1, 1, 1, 1);
        CGContextFillRect(bitmapContext, CGRectMake(0, 0, width, height));
        CGContextSetInterpolationQuality(bitmapContext, kCGInterpolationNone);
        CGContextDrawImage(
            bitmapContext,
            CGRectMake(padding, padding, CGImageGetWidth(qrImage), CGImageGetHeight(qrImage)),
            qrImage
        );
        CGImageRef canvasImage = CGBitmapContextCreateImage(bitmapContext);
        CGContextRelease(bitmapContext);
        CGImageRelease(qrImage);
        if (!canvasImage) {
            fprintf(stderr, "could not create QR canvas\n");
            return 2;
        }
        NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc] initWithCGImage:canvasImage];
        CGImageRelease(canvasImage);
        NSData *png = [bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
        if (!png || ![png writeToFile:outputPath atomically:YES]) {
            fprintf(stderr, "could not write QR image\n");
            return 2;
        }
    }
    return 0;
}
