% main_pipeline.m — MATLAB sample: top-level pipeline script

function run_pipeline(input_file, output_dir)
% Run the full signal processing pipeline on an input file.
    data = load_data(input_file);
    fs   = 1000;  % Hz

    result = process_signal(data, fs);

    summary = build_summary(result, input_file);
    save_results(summary, output_dir);
    log_message('Pipeline complete.');
end

function data = load_data(filepath)
% Load signal data from a .mat or .csv file.
    [~, ~, ext] = fileparts(filepath);
    if strcmpi(ext, '.mat')
        tmp  = load(filepath);
        data = tmp.data;
    else
        data = csvread(filepath);
    end
end

function summary = build_summary(result, label)
    summary = struct();
    summary.label    = label;
    summary.rms      = result.rms;
    summary.dom_freq = result.dom_freq;
    summary.timestamp = datestr(now);
end

function save_results(summary, output_dir)
    filename = fullfile(output_dir, 'results.mat');
    save(filename, 'summary');
    log_message(['Saved: ' filename]);
end

function log_message(msg)
    fprintf('[%s] %s\n', datestr(now, 'HH:MM:SS'), msg);
end
