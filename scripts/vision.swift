import Foundation
import Vision
import AppKit

struct Line: Codable { let text: String; let score: Float; let box: [Double] }
guard CommandLine.arguments.count >= 2 else { fatalError("image path required") }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: url), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { fatalError("unreadable image") }
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = false
request.usesCPUOnly = true
if CommandLine.arguments.count >= 3 { request.recognitionLevel = .fast }
do { try VNImageRequestHandler(cgImage: cg).perform([request]) }
catch { FileHandle.standardError.write(Data("Vision failed: \(error)\n".utf8)); exit(2) }
let w = Double(cg.width), h = Double(cg.height)
let lines = (request.results ?? []).compactMap { o -> Line? in
    guard let candidate = o.topCandidates(1).first else { return nil }
    let b = o.boundingBox
    return Line(text: candidate.string, score: candidate.confidence,
                box: [b.minX*w, (1-b.maxY)*h, b.maxX*w, (1-b.minY)*h])
}
let data = try JSONEncoder().encode(lines)
FileHandle.standardOutput.write(data)
