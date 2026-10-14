// Headless feasibility driver for engine-sim (no GUI, no delta-studio).
// Usage: headless_driver <path/to/main.mr> [seconds] [throttle 0..1] [dyno_rpm or 0]
// Must be run from a directory where ../es/ or es/ resolves to engine-sim/es.
#include "engine.h"
#include "simulator.h"
#include "units.h"
#include "../scripting/include/compiler.h"

#include "impulse_response.h"
#include "exhaust_system.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <vector>

// Minimal RIFF/PCM16 reader replacing delta-studio's Windows-only ysWindowsAudioWaveFile.
static std::vector<int16_t> readWav16(const std::string &path) {
    std::ifstream f(path, std::ios::binary);
    std::vector<int16_t> out;
    if (!f) return out;
    char hdr[12];
    f.read(hdr, 12);
    if (std::memcmp(hdr, "RIFF", 4) != 0 || std::memcmp(hdr + 8, "WAVE", 4) != 0) return out;
    char id[4]; uint32_t sz;
    while (f.read(id, 4) && f.read(reinterpret_cast<char *>(&sz), 4)) {
        if (std::memcmp(id, "data", 4) == 0) {
            out.resize(sz / 2);
            f.read(reinterpret_cast<char *>(out.data()), sz);
            break;
        }
        f.seekg(sz + (sz & 1), std::ios::cur);
    }
    return out;
}

static void writeWav16(const std::string &path, const std::vector<int16_t> &pcm, uint32_t rate) {
    std::ofstream f(path, std::ios::binary);
    const uint32_t dataBytes = (uint32_t)pcm.size() * 2, riff = 36 + dataBytes, fmtSize = 16,
                   byteRate = rate * 2;
    const uint16_t fmt = 1, ch = 1, align = 2, bits = 16;
    f.write("RIFF", 4); f.write((const char *)&riff, 4); f.write("WAVEfmt ", 8);
    f.write((const char *)&fmtSize, 4); f.write((const char *)&fmt, 2); f.write((const char *)&ch, 2);
    f.write((const char *)&rate, 4); f.write((const char *)&byteRate, 4);
    f.write((const char *)&align, 2); f.write((const char *)&bits, 2);
    f.write("data", 4); f.write((const char *)&dataBytes, 4);
    f.write((const char *)pcm.data(), dataBytes);
}

int main(int argc, char **argv) {
    const char *script = argc > 1 ? argv[1] : "../assets/main.mr";
    const double seconds = argc > 2 ? atof(argv[2]) : 5.0;
    const double throttle = argc > 3 ? atof(argv[3]) : 1.0;
    const double dynoRpm = argc > 4 ? atof(argv[4]) : 0.0;

    es_script::Compiler compiler;
    compiler.initialize();
    if (!compiler.compile(script)) {
        fprintf(stderr, "script compile failed (see error_log.log)\n");
        return 1;
    }
    es_script::Compiler::Output out = compiler.execute();
    compiler.destroy();
    if (out.engine == nullptr || out.vehicle == nullptr || out.transmission == nullptr) {
        fprintf(stderr, "script produced no engine/vehicle/transmission\n");
        return 2;
    }

    Engine *engine = out.engine;
    Simulator *sim = engine->createSimulator(out.vehicle, out.transmission);
    engine->calculateDisplacement();
    sim->setSimulationFrequency(engine->getSimulationFrequency());
    // The app loads exhaust impulse responses with delta-studio's Windows-only WAV reader;
    // without them the synthesizer's convolution filter is uninitialised and crashes.
    for (int i = 0; i < engine->getExhaustSystemCount(); ++i) {
        ImpulseResponse *ir = engine->getExhaustSystem(i)->getImpulseResponse();
        std::vector<int16_t> pcm = readWav16(ir->getFilename());
        if (pcm.empty()) { pcm.assign(1, INT16_MAX); }  // Dirac fallback
        sim->synthesizer().initializeImpulseResponse(pcm.data(), (unsigned)pcm.size(),
                                                     (float)ir->getVolume(), i);
        printf("impulse response %d: %s (%zu samples)\n", i, ir->getFilename().c_str(), pcm.size());
    }
    sim->startAudioRenderingThread();

    printf("engine=%s cylinders=%d displacement_L=%.3f sim_freq=%d\n",
           engine->getName().c_str(), engine->getCylinderCount(),
           engine->getDisplacement() / units::volume(1.0, units::L),
           sim->getSimulationFrequency());

    engine->getIgnitionModule()->m_enabled = true;
    engine->setSpeedControl(throttle);
    if (dynoRpm > 0) {
        sim->m_dyno.m_enabled = true;
        sim->m_dyno.m_hold = true;
        sim->m_dyno.m_rotationSpeed = units::rpm(dynoRpm);
    }

    const double frameDt = 1.0 / 60.0;
    const int frames = static_cast<int>(seconds / frameDt);
    std::vector<int16_t> audio(44100), allAudio;
    long long totalSteps = 0, totalAudio = 0; double lastAvgTq = 0;
    for (int f = 0; f < frames; ++f) {
        sim->m_starterMotor.m_enabled = (f * frameDt) < 1.0;  // crank for 1 s
        sim->startFrame(frameDt);
        double tq = 0; int n = 0;
        while (sim->simulateStep()) { ++totalSteps; tq += sim->m_dyno.getTorque(); ++n; }
        lastAvgTq = n ? tq / n : 0;
        sim->endFrame();
        const int got = sim->readAudioOutput(static_cast<int>(audio.size()), audio.data());
        allAudio.insert(allAudio.end(), audio.begin(), audio.begin() + got);
        totalAudio += got;
        if (f % 30 == 0) {
            printf("t=%.2fs steps=%lld rpm=%.0f map_kPa=%.1f afr=%.2f dyno_torque_Nm=%.1f "
                   "step_avg_dyno_Nm=%.1f dyno_power_kW=%.1f latency=%.3f audio_samples=%lld\n",
                   f * frameDt, totalSteps, engine->getRpm(),
                   engine->getManifoldPressure() / 1000.0, engine->getIntakeAfr(),
                   sim->getFilteredDynoTorque(), lastAvgTq, sim->getDynoPower() / 1000.0,
                   sim->getSynthesizerInputLatency(), totalAudio);
        }
    }

    sim->endAudioRenderingThread();
    writeWav16("headless_out.wav", allAudio, 44100);
    printf("done: total physics steps=%lld (%.1f s simulated at %d Hz)\n", totalSteps,
           totalSteps / (double)sim->getSimulationFrequency(), sim->getSimulationFrequency());
    return 0;
}
