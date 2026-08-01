import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 2 else {
    fputs("usage: vision_ocr <image>\n", stderr)
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard
    let image = NSImage(contentsOf: imageURL),
    let data = image.tiffRepresentation,
    let bitmap = NSBitmapImageRep(data: data),
    let cgImage = bitmap.cgImage
else {
    fputs("unable to decode image\n", stderr)
    exit(3)
}

let chunkHeight = 2000
let overlap = 100
var start = 0
var lines: [String] = []

while start < cgImage.height {
    let end = min(start + chunkHeight, cgImage.height)
    let crop = CGRect(x: 0, y: start, width: cgImage.width, height: end - start)
    guard let slice = cgImage.cropping(to: crop) else {
        fputs("unable to crop image\n", stderr)
        exit(4)
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    do {
        try VNImageRequestHandler(cgImage: slice).perform([request])
        lines.append(contentsOf: (request.results ?? []).compactMap {
            $0.topCandidates(1).first?.string
        })
    } catch {
        fputs("vision request failed: \(error)\n", stderr)
        exit(5)
    }
    if end == cgImage.height {
        break
    }
    start = end - overlap
}

print(lines.joined(separator: "\n"))
