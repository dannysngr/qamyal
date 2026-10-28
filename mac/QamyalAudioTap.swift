// Qamyal: звук компьютера в macOS (собеседники в «Созвоне», «Звук ПК»).
// Core Audio process tap (macOS 14.2+) снимает весь звук, который играет система, — без виртуальных
// аудиоустройств и без записи экрана. Разрешение: «Запись системного звука» (NSAudioCaptureUsageDescription
// в Info.plist оболочки Qamyal.app). Пишет в stdout моно float32 с заданной частотой, пока его не остановят.
//
// Сборка:  swiftc -O QamyalAudioTap.swift -o QamyalAudioTap
// Запуск:  QamyalAudioTap 16000 > out.f32
//
// Основано на схеме из примера Apple «Capturing system audio with Core Audio taps»:
// CATapDescription → AudioHardwareCreateProcessTap → приватное aggregate-устройство с этим tap → IOProc.

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

setvbuf(stdout, nil, _IOFBF, 1 << 16)
signal(SIGPIPE, SIG_IGN)

func fail(_ msg: String, _ status: OSStatus = noErr) -> Never {
    let tail = status == noErr ? "" : " (код \(status))"
    FileHandle.standardError.write("\(msg)\(tail)\n".data(using: .utf8)!)
    exit(1)
}

guard #available(macOS 14.2, *) else { fail("запись звука компьютера требует macOS 14.2 или новее") }

let outRate = Double(CommandLine.arguments.dropFirst().first ?? "16000") ?? 16000

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func defaultOutputUID() -> String? {
    var device = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    var addr = address(kAudioHardwarePropertyDefaultSystemOutputDevice)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &device) == noErr
    else { return nil }
    var uid: Unmanaged<CFString>?
    size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    addr = address(kAudioDevicePropertyDeviceUID)
    guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, &uid) == noErr, let u = uid else { return nil }
    return u.takeRetainedValue() as String
}

// 1. tap всего системного звука (стерео-сведение), ничего не исключаем; звук у пользователя не глушим
let tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDesc.uuid = UUID()
tapDesc.isPrivate = true
tapDesc.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
var status = AudioHardwareCreateProcessTap(tapDesc, &tapID)
guard status == noErr else {
    fail("не удалось подключиться к звуку компьютера — разрешите Qamyal «Запись системного звука» " +
         "в Системных настройках → Конфиденциальность и безопасность", status)
}

// 2. формат, в котором tap отдаёт звук
var tapFormat = AudioStreamBasicDescription()
var fmtSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
var fmtAddr = address(kAudioTapPropertyFormat)
status = AudioObjectGetPropertyData(tapID, &fmtAddr, 0, nil, &fmtSize, &tapFormat)
guard status == noErr, let inFormat = AVAudioFormat(streamDescription: &tapFormat) else {
    fail("не удалось узнать формат звука", status)
}

// 3. приватное aggregate-устройство: основное устройство вывода + наш tap
guard let outUID = defaultOutputUID() else { fail("не найдено устройство вывода звука") }
let aggregate: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Qamyal",
    kAudioAggregateDeviceUIDKey: UUID().uuidString,
    kAudioAggregateDeviceMainSubDeviceKey: outUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                       kAudioSubTapUIDKey: tapDesc.uuid.uuidString]],
]
var aggID = AudioObjectID(kAudioObjectUnknown)
status = AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggID)
guard status == noErr else { fail("не удалось создать устройство захвата", status) }

// 4. пересчёт в моно float32 нужной частоты и запись в stdout
guard let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outRate, channels: 1, interleaved: true),
      let converter = AVAudioConverter(from: inFormat, to: outFormat)
else { fail("не удалось настроить пересчёт звука") }

func cleanupAndExit(_ code: Int32) -> Never {
    AudioHardwareDestroyAggregateDevice(aggID)
    AudioHardwareDestroyProcessTap(tapID)
    exit(code)
}

let queue = DispatchQueue(label: "qamyal.tap")
var procID: AudioDeviceIOProcID?
status = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, queue) { _, inInputData, _, _, _ in
    guard let input = AVAudioPCMBuffer(pcmFormat: inFormat, bufferListNoCopy: inInputData, deallocator: nil),
          input.frameLength > 0 else { return }
    let capacity = AVAudioFrameCount(Double(input.frameLength) * outRate / inFormat.sampleRate) + 32
    guard let output = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
    var fed = false
    var error: NSError?
    converter.convert(to: output, error: &error) { _, inputStatus in
        if fed { inputStatus.pointee = .noDataNow; return nil }
        fed = true
        inputStatus.pointee = .haveData
        return input
    }
    guard error == nil, output.frameLength > 0, let data = output.floatChannelData else { return }
    let n = Int(output.frameLength)
    if fwrite(data[0], MemoryLayout<Float>.size, n, stdout) < n || fflush(stdout) != 0 {
        cleanupAndExit(0)                 // программа закрыла канал — нас больше не слушают
    }
}
guard status == noErr, let proc = procID else { fail("не удалось подключить обработчик звука", status) }
status = AudioDeviceStart(aggID, proc)
guard status == noErr else { fail("не удалось начать запись звука компьютера", status) }

// остановка по SIGTERM / SIGINT: убираем tap и устройство, иначе они висят до перезагрузки звука
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        AudioDeviceStop(aggID, proc)
        AudioDeviceDestroyIOProcID(aggID, proc)
        cleanupAndExit(0)
    }
    src.resume()
    _ = Unmanaged.passRetained(src)
}
dispatchMain()
