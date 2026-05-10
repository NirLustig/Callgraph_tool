% signal_processor.m — MATLAB sample: signal processing functions

function result = process_signal(data, fs)
% Process a raw signal: filter, normalize, then compute features.
    filtered = apply_filter(data, fs);
    normed   = normalize_signal(filtered);
    result   = extract_features(normed, fs);
end

function out = apply_filter(data, fs)
% Apply a simple low-pass FIR filter.
    cutoff = fs / 4;
    win = hamming(64);
    b   = fir1(63, cutoff / (fs/2), win);
    out = filter(b, 1, data);
end

function normed = normalize_signal(data)
% Normalize signal to [-1, 1] range.
    mn  = min(data);
    mx  = max(data);
    rng = mx - mn;
    if rng == 0
        normed = zeros(size(data));
    else
        normed = 2 * (data - mn) / rng - 1;
    end
end

function feats = extract_features(data, fs)
% Extract RMS energy and dominant frequency.
    rms_val  = compute_rms(data);
    dom_freq = find_dominant_freq(data, fs);
    feats    = struct('rms', rms_val, 'dom_freq', dom_freq);
end

function r = compute_rms(data)
    r = sqrt(mean(data .^ 2));
end

function freq = find_dominant_freq(data, fs)
    N    = length(data);
    Y    = fft(data);
    P    = abs(Y(1:N/2+1)).^2 / N;
    f    = (0:N/2) * fs / N;
    [~, idx] = max(P);
    freq = f(idx);
end
