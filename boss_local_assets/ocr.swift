import Foundation
import Vision

do {
    guard CommandLine.arguments.count == 2 else {
        throw NSError(domain: "boss-local-mcp", code: 1, userInfo: [NSLocalizedDescriptionKey: "Expected one image path"])
    }
    let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
    let recognition = VNRecognizeTextRequest()
    recognition.recognitionLevel = .accurate
    recognition.recognitionLanguages = ["zh-Hans", "en-US"]
    recognition.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(url: imageURL, options: [:])
    try handler.perform([recognition])
    let lines = (recognition.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    let payload: [String: Any] = ["text": lines.joined(separator: "\n"), "engine": "apple-vision"]
    let data = try JSONSerialization.data(withJSONObject: payload)
    print(String(decoding: data, as: UTF8.self))
} catch {
    FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
    exit(1)
}
