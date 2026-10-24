// ocr_pdf — macOS Vision OCR für PDF-Seiten (kein Fremdpaket, PDFKit + Vision)
// Aufruf: ocr_pdf <datei.pdf> [maxSeiten] [skalierung] [seiten]
//   seiten: komma-getrennte 1-basierte Auswahl, z.B. "5" oder "3,7-9".
//           Ohne Auswahl: Seiten 1..maxSeiten (0 = alle).
import Foundation
import PDFKit
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: ocr_pdf <file.pdf> [maxPages] [scale] [pages]\n".data(using: .utf8)!)
    exit(2)
}
let path = args[1]
let maxPages = args.count > 2 ? Int(args[2]) ?? 0 : 0
let scale = args.count > 3 ? CGFloat(Double(args[3]) ?? 2.0) : 2.0
let pagesArg = args.count > 4 ? args[4].trimmingCharacters(in: .whitespaces) : ""

// "3,7-9" -> [3,7,8,9]
func parsePages(_ spec: String) -> Set<Int> {
    var out = Set<Int>()
    for part in spec.split(separator: ",") {
        let p = part.trimmingCharacters(in: .whitespaces)
        if p.isEmpty { continue }
        if p.contains("-") {
            let b = p.split(separator: "-", maxSplits: 1).compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }
            if b.count == 2, b[0] <= b[1] {
                for n in b[0]...b[1] { out.insert(n) }
            }
        } else if let n = Int(p) {
            out.insert(n)
        }
    }
    return out
}

let selection = pagesArg.isEmpty ? nil : parsePages(pagesArg)

guard let doc = PDFDocument(url: URL(fileURLWithPath: path)) else {
    FileHandle.standardError.write("cannot open PDF\n".data(using: .utf8)!)
    exit(3)
}

let pageCount = doc.pageCount
let limit = maxPages > 0 ? min(maxPages, pageCount) : pageCount

for i in 0..<limit {
    let pageNo = i + 1
    if let sel = selection, !sel.contains(pageNo) { continue }
    guard let page = doc.page(at: i) else { continue }
    let rect = page.bounds(for: .mediaBox)
    let w = Int(rect.width * scale)
    let h = Int(rect.height * scale)
    guard w > 0, h > 0 else { continue }

    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h,
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                     isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0) else { continue }
    rep.size = NSSize(width: w, height: h)

    guard let ctx = NSGraphicsContext(bitmapImageRep: rep) else { continue }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx
    let cg = ctx.cgContext
    cg.setFillColor(NSColor.white.cgColor)
    cg.fill(CGRect(x: 0, y: 0, width: w, height: h))
    cg.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: cg)
    NSGraphicsContext.restoreGraphicsState()

    guard let cgImage = rep.cgImage else { continue }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["de-DE", "en-US"]
    request.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        FileHandle.standardError.write("vision error page \(i + 1): \(error)\n".data(using: .utf8)!)
        continue
    }

    guard let observations = request.results else { continue }
    print("--- Seite \(i + 1) ---")
    for obs in observations {
        if let top = obs.topCandidates(1).first {
            print(top.string)
        }
    }
}
