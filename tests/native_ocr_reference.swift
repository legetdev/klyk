// Compare Vision's typed native API against PyObjC using only one tiny generated PNG.
import Foundation
import CoreGraphics
import ImageIO
import Vision
import CoreML
import CryptoKit

let resultPrefix = "KLYK_NATIVE_OCR_MODE_RESULT "

/// Read thread identity through the synchronous API before recording async request metadata.
func threadMetadata() -> [String: Any] {
    ["is_main": Thread.isMainThread]
}

/// Read scalar settings without warming supported-model queries before recognition.
func settings(_ request: VNRecognizeTextRequest) -> [String: Any] {
    ["level": request.recognitionLevel.rawValue, "revision": request.revision,
     "language_correction": request.usesLanguageCorrection, "languages": request.recognitionLanguages]
}

/// Select the same advertised CPU stages as production, while keeping typed native values.
func configureCPU(_ request: VNRecognizeTextRequest) throws {
    if #available(macOS 14, *) {
        for (stage, devices) in try request.supportedComputeStageDevices {
            for device in devices {
                if case .cpu = device {
                    request.setComputeDevice(device, for: stage)
                    break
                }
            }
        }
    }
}

/// Preserve typed native error details instead of replacing a failure with empty success.
func errorDetail(_ error: Error) -> [String: Any] {
    let value = error as NSError
    return ["type": String(describing: type(of: error)), "domain": value.domain,
            "code": value.code, "message": String(value.localizedDescription.prefix(2000)),
            "description": String(String(describing: error).prefix(2000))]
}

/// Read supported models and devices only after the actual request has returned.
func metadata(_ request: VNRecognizeTextRequest) -> [String: Any] {
    var result = settings(request)
    result["supported_revisions"] = Array(VNRecognizeTextRequest.supportedRevisions).prefix(10).map { $0 }
    do {
        result["supported_languages"] = Array(try request.supportedRecognitionLanguages().prefix(100))
    } catch { result["supported_language_error"] = errorDetail(error) }
    if #available(macOS 14, *) {
        do {
            result["compute_stages"] = try request.supportedComputeStageDevices.map { pair -> [String: Any] in
                let (stage, devices) = pair
                return ["stage": stage.rawValue, "supported_devices": devices.map { String(describing: $0) },
                        "assigned_device": request.computeDevice(for: stage).map { String(describing: $0) as Any } ?? NSNull()]
            }
        } catch { result["compute_error"] = errorDetail(error) }
    }
    return result
}

/// Inspect modern scalar settings without querying supported devices before recognition.
@available(macOS 15, *)
func modernSettings(_ request: RecognizeTextRequest) -> [String: Any] {
    ["level": request.recognitionLevel == .fast ? 1 : 0, "revision": String(describing: request.revision),
     "language_correction": request.usesLanguageCorrection,
     "languages": request.recognitionLanguages.map { $0.minimalIdentifier },
     "automatically_detects_language": request.automaticallyDetectsLanguage]
}

/// Isolate modern Vision's async API on the same private image without changing production.
@available(macOS 15, *)
@MainActor
func modernReport(image: CGImage, level: UInt, initial: [String: Any]) async -> [String: Any] {
    var report = initial
    report["api"] = "RecognizeTextRequest"
    var request = RecognizeTextRequest()
    report["initial_request"] = modernSettings(request)
    if level == 1 { request.recognitionLevel = .fast }
    report["final_request"] = modernSettings(request)
    let started = ProcessInfo.processInfo.systemUptime
    do {
        let results = try await request.perform(on: image)
        report["native_success"] = true
        report["native_error"] = NSNull()
        report["native_results_nil"] = false
        report["native_result_count"] = results.count
        let observations: [[String: Any]] = results.prefix(10).compactMap { item in
            guard let candidate = item.topCandidates(1).first else { return nil }
            let box = item.boundingBox.cgRect
            return ["text": String(candidate.string.prefix(2000)), "confidence": candidate.confidence,
                    "x": Int((box.minX + box.width / 2) * 512),
                    "y": Int((1 - box.minY - box.height / 2) * 256),
                    "width": Int(box.width * 512), "height": Int(box.height * 256)]
        }
        report["observations"] = observations
        report["passed"] = observations.contains {
            ($0["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() == "HELLO"
        }
    } catch {
        report["native_success"] = false
        report["native_error"] = errorDetail(error)
        report["native_results_nil"] = true
        report["native_result_count"] = NSNull()
    }
    report["native_elapsed_seconds"] = ProcessInfo.processInfo.systemUptime - started
    report["native_performed"] = true
    report["completed"] = true
    var after = modernSettings(request)
    after["supported_languages"] = request.supportedRecognitionLanguages.prefix(100).map { $0.minimalIdentifier }
    after["compute_stages"] = request.supportedComputeStageDevices.map { pair -> [String: Any] in
        let (stage, devices) = pair
        return ["stage": String(describing: stage), "supported_devices": devices.map { String(describing: $0) },
                "assigned_device": request.computeDevice(for: stage).map { String(describing: $0) as Any } ?? NSNull()]
    }
    report["native_metadata_after_request"] = after
    return report
}

/// Perform one bounded reference on the exact fixture, without any desktop or app APIs.
@MainActor
func run() async -> (Int32, [String: Any]) {
    var report: [String: Any] = ["completed": false, "passed": false, "native_performed": false,
                               "backend": "swift", "platform": ProcessInfo.processInfo.operatingSystemVersionString,
                               "thread": threadMetadata()]
    do {
        guard CommandLine.arguments.count == 6,
              let level = UInt(CommandLine.arguments[4]), (0...1).contains(level),
              let revision = UInt(CommandLine.arguments[5]), (0...3).contains(revision),
              ["defaults", "production", "modern"].contains(CommandLine.arguments[3]) else {
            throw NSError(domain: "KlykGeneratedDiagnostic", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Invalid typed reference arguments."])
        }
        report["case_name"] = CommandLine.arguments[1]
        let file = try FileHandle(forReadingFrom: URL(fileURLWithPath: CommandLine.arguments[2]))
        defer { try? file.close() }
        let data = try file.read(upToCount: 65537) ?? Data()
        guard data.count <= 65536, data.count >= 33,
              Array(data.prefix(8)) == [137, 80, 78, 71, 13, 10, 26, 10],
              Array(data[16..<24]) == [0, 0, 2, 0, 0, 0, 1, 0],
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              CGImageSourceGetType(source).map({ $0 as String }) == "public.png",
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
              image.width == 512, image.height == 256 else {
            throw NSError(domain: "KlykGeneratedDiagnostic", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "Only the generated 512 by 256 PNG fixture is accepted."])
        }
        report["input_sha256"] = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        report["image"] = ["width": image.width, "height": image.height,
                           "bits_per_component": image.bitsPerComponent, "bits_per_pixel": image.bitsPerPixel,
                           "alpha_info": image.alphaInfo.rawValue]
        if CommandLine.arguments[3] == "modern" {
            if #available(macOS 15, *) {
                report = await modernReport(image: image, level: level, initial: report)
                return (report["passed"] as? Bool == true ? 0 : 1, report)
            }
            throw NSError(domain: "KlykGeneratedDiagnostic", code: 4,
                          userInfo: [NSLocalizedDescriptionKey: "The modern diagnostic requires macOS 15 or newer."])
        }
        report["api"] = "VNRecognizeTextRequest"
        let request = VNRecognizeTextRequest()
        report["initial_request"] = settings(request)
        if revision > 0 {
            let supported = VNRecognizeTextRequest.supportedRevisions.contains(Int(revision))
            report["revision_supported"] = supported
            guard supported else {
                throw NSError(domain: "KlykGeneratedDiagnostic", code: 3,
                              userInfo: [NSLocalizedDescriptionKey: "The diagnostic revision is not advertised as supported."])
            }
            request.revision = Int(revision)
        }
        if CommandLine.arguments[3] == "production" {
            request.recognitionLevel = level == 0 ? .accurate : .fast
            try configureCPU(request)
            request.usesLanguageCorrection = false
            request.recognitionLanguages = ["en-US"]
        }
        report["final_request"] = settings(request)
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        let started = ProcessInfo.processInfo.systemUptime
        do {
            try handler.perform([request])
            report["native_success"] = true
            report["native_error"] = NSNull()
        } catch {
            report["native_success"] = false
            report["native_error"] = errorDetail(error)
        }
        report["native_elapsed_seconds"] = ProcessInfo.processInfo.systemUptime - started
        report["native_performed"] = true
        report["completed"] = true
        report["native_results_nil"] = request.results == nil
        report["native_result_count"] = request.results.map { $0.count as Any } ?? NSNull()
        report["native_metadata_after_request"] = metadata(request)
        let observations: [[String: Any]] = (request.results ?? []).prefix(10).compactMap { item in
            guard let candidate = item.topCandidates(1).first else { return nil }
            let box = item.boundingBox
            return ["text": String(candidate.string.prefix(2000)), "confidence": candidate.confidence,
                    "x": Int((box.minX + box.width / 2) * 512),
                    "y": Int((1 - box.minY - box.height / 2) * 256),
                    "width": Int(box.width * 512), "height": Int(box.height * 256)]
        }
        report["observations"] = observations
        report["passed"] = report["native_success"] as? Bool == true && observations.contains {
            ($0["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() == "HELLO"
        }
    } catch { report["failure"] = errorDetail(error) }
    return (report["passed"] as? Bool == true ? 0 : 1, report)
}

let (exitCode, report) = await run()
do {
    let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
    print(resultPrefix + String(decoding: data, as: UTF8.self))
    exit(exitCode)
} catch {
    print(resultPrefix + "{\"completed\":false,\"passed\":false,\"failure\":{\"message\":\"Native JSON encoding failed.\"}}")
    exit(1)
}
