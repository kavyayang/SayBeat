// Offline audio only. No microphone, playback device or network access.
#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>
#include <math.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 4) return 1;
        float rate = atof(argv[3]);
        if (!isfinite(rate) || rate < .8 || rate > 1.25) return 1;
        NSError *error = nil;
        AVAudioFile *source = [[AVAudioFile alloc] initForReading:
            [NSURL fileURLWithPath:@(argv[1])] error:&error];
        if (!source || error) return 1;
        AVAudioEngine *engine = [AVAudioEngine new];
        AVAudioPlayerNode *player = [AVAudioPlayerNode new];
        AVAudioUnitTimePitch *effect = [AVAudioUnitTimePitch new];
        effect.rate = rate;
        effect.pitch = 0;
        [engine attachNode:player];
        [engine attachNode:effect];
        [engine connect:player to:effect format:source.processingFormat];
        [engine connect:effect to:engine.mainMixerNode format:source.processingFormat];
        if (![engine enableManualRenderingMode:AVAudioEngineManualRenderingModeOffline
            format:source.processingFormat maximumFrameCount:4096 error:&error]) return 1;
        AVAudioFile *output = [[AVAudioFile alloc] initForWriting:
            [NSURL fileURLWithPath:@(argv[2])] settings:engine.manualRenderingFormat.settings error:&error];
        if (!output || error) return 1;
        AVAudioPCMBuffer *buffer = [[AVAudioPCMBuffer alloc] initWithPCMFormat:
            engine.manualRenderingFormat frameCapacity:4096];
        [player scheduleFile:source atTime:nil completionHandler:nil];
        if (![engine startAndReturnError:&error]) return 1;
        [player play];
        // Render the entire source and one extra second for the processor's tail.
        // No selected phrases, watermark or waveform ending are cropped.
        AVAudioFramePosition frames = ceil((double)source.length / rate) + source.processingFormat.sampleRate;
        unsigned stalled = 0;
        while (engine.manualRenderingSampleTime < frames) {
            AVAudioFrameCount count = (AVAudioFrameCount)MIN(4096, frames-engine.manualRenderingSampleTime);
            AVAudioEngineManualRenderingStatus status = [engine renderOffline:count toBuffer:buffer error:&error];
            if (status == AVAudioEngineManualRenderingStatusSuccess) {
                if (![output writeFromBuffer:buffer error:&error]) return 1;
                stalled = 0;
            } else if (status != AVAudioEngineManualRenderingStatusCannotDoInCurrentContext || ++stalled > 1000) {
                return 1;
            }
        }
        [player stop];
        [engine stop];
        return 0;
    }
}
