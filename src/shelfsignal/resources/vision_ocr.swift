import Foundation
import ImageIO
import Vision

let maxFileBytes = 25 * 1024 * 1024
let maxDimension = 50_000
let maxPixels = 40_000_000
let chunkHeight = 2000
let overlap = 100

func sortedLines(_ observations: [VNRecognizedTextObservation]) -> [String] {
    return observations.compactMap { observation -> (String, CGFloat, CGFloat)? in
        guard let text = observation.topCandidates(1).first?.string else {
            return nil
        }
        return (text, observation.boundingBox.maxY, observation.boundingBox.minX)
    }.sorted { left, right in
        if left.1 != right.1 {
            return left.1 > right.1
        }
        if left.2 != right.2 {
            return left.2 < right.2
        }
        return left.0 < right.0
    }.map { $0.0 }
}

func appendMergingOverlap(_ accumulated: inout [String], next: [String]) {
    let maximum = min(accumulated.count, next.count, 32)
    var duplicateCount = 0
    if maximum > 0 {
        for count in stride(from: maximum, through: 1, by: -1) {
            if Array(accumulated.suffix(count)) == Array(next.prefix(count)) {
                duplicateCount = count
                break
            }
        }
    }
    accumulated.append(contentsOf: next.dropFirst(duplicateCount))
}

guard CommandLine.arguments.count == 2 else {
    fputs("usage: vision_ocr <image>\n", stderr)
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard
    let attributes = try? FileManager.default.attributesOfItem(atPath: imageURL.path),
    let fileBytes = (attributes[.size] as? NSNumber)?.intValue,
    fileBytes >= 0,
    fileBytes <= maxFileBytes,
    let imageSource = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
    let properties = CGImageSourceCopyPropertiesAtIndex(imageSource, 0, nil) as NSDictionary?,
    let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue,
    let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue,
    width > 0,
    height > 0,
    width <= maxDimension,
    height <= maxDimension,
    width * height <= maxPixels
else {
    fputs("image exceeds safe decode limits or has invalid metadata\n", stderr)
    exit(3)
}

guard let cgImage = CGImageSourceCreateImageAtIndex(imageSource, 0, nil) else {
    fputs("unable to decode image\n", stderr)
    exit(4)
}

var start = 0
var lines: [String] = []

while start < cgImage.height {
    let end = min(start + chunkHeight, cgImage.height)
    let crop = CGRect(x: 0, y: start, width: cgImage.width, height: end - start)
    guard let slice = cgImage.cropping(to: crop) else {
        fputs("unable to crop image\n", stderr)
        exit(5)
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    do {
        try VNImageRequestHandler(cgImage: slice).perform([request])
        appendMergingOverlap(&lines, next: sortedLines(request.results ?? []))
    } catch {
        fputs("vision request failed: \(error)\n", stderr)
        exit(6)
    }
    if end == cgImage.height {
        break
    }
    start = end - overlap
}

print(lines.joined(separator: "\n"))
