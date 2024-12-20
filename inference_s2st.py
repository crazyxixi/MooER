import time
import torch
try:
    import torch_musa
except ImportError as e:
    print("You should install torch_musa if you want to run on Moore Threads GPU")
import os
import argparse
import torchaudio
from torchaudio.transforms import Resample
import logging
from mooer.datasets.speech_processor import *
from mooer.configs import asr_config
from mooer.models import mooer_model
from mooer.utils.utils import *
from mooer.models.hifigan import save_wav, get_hifigan_model, get_speaker_encoder, encode_prompt_wav

parser = argparse.ArgumentParser()
parser.add_argument("--wav_path", default='demo/resources/demo.wav', type=str, help="decode one wav file")
parser.add_argument("--wav_scp", default=None, type=str, help="decode scp if you want")
parser.add_argument("--task", default='s2s_chat', choices=['asr', 'ast', 's2s_trans', 's2s_chat'],
                    type=str, help="task: asr or ast or s2s_trans or s2s_chat. "
                                   "Please set ast if you choose a asr/ast/s2s_trans/s2s_chat multitask model")
parser.add_argument("--batch_size", default=1, type=int, help="decode batch for scp")
parser.add_argument("--cmvn_path", default='', type=str, help="cmvn path.")
parser.add_argument("--encoder_path", default='', type=str, help="encoder path.")
parser.add_argument("--llm_path", default='', type=str, help="llm path.")
parser.add_argument("--adapter_path", default='', type=str, help="adapter path.")
parser.add_argument("--lora_dir", default='', type=str, help="lora path.")
parser.add_argument("--vocoder_path", default='', type=str, help="vocoder path")
parser.add_argument("--spk_encoder_path", default='', type=str, help="spk encoder path")
parser.add_argument("--prompt_wav_path", default='', type=str, help="prompt wav path")
parser.add_argument("--output_dir", default="response_wavs_dir", type=str, help="path to save wav generated")
args = parser.parse_args()

assert args.batch_size == 1, "Only support bsz=1 for S2ST task now. We will support batch inference soon."

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    filemode='w'
)

PROMPT_TEMPLATE_DICT = {
    'qwen': "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n{}<|im_end|>\n<|im_start|>assistant\n",
}
PROMPT_DICT = {
    'asr': "Transcribe speech to text. ",
    'ast': "Translate speech to english text. ",
    's2s_trans': "Translate speech to english speech. ",
    's2s_chat': "Answer my question with speech. "
}

model_config = asr_config.ModelConfig()

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# replace path
if args.llm_path and os.path.exists(args.llm_path):
    model_config.llm_path = args.llm_path
if args.encoder_path and os.path.exists(args.encoder_path):
    model_config.encoder_path = args.encoder_path
if args.adapter_path and os.path.exists(args.adapter_path):
    model_config.adapter_path = args.adapter_path
if args.lora_dir and os.path.exists(args.lora_dir):
    model_config.lora_dir = args.lora_dir
if args.cmvn_path and os.path.exists(args.cmvn_path):
    model_config.cmvn_path = args.cmvn_path
if args.task:
    model_config.prompt_key = args.task
    

device = str(get_device())
logger.info("This demo will run on {}".format(device.upper()))

logger.info(model_config)

os.makedirs(args.output_dir, exist_ok=True)
logger.info("Response wav will save in {}".format(args.output_dir))

model, tokenizer = mooer_model.init_model(
    model_config=model_config)
AUDIO_START_TOKEN_INDEX = tokenizer.get_vocab()['<|audio_start|>']
model.to(device)
model.eval()

# data process
prompt_template_key = model_config.get('prompt_template_key', 'qwen')
prompt_template = PROMPT_TEMPLATE_DICT[prompt_template_key]
prompt_key = model_config.get('prompt_key', 'asr')
prompt_org = PROMPT_DICT[prompt_key]

logger.info(f"Use LLM Type {prompt_template_key}, "
             f"Prompt template {prompt_template}, "
             f"Use task type {prompt_key}, "
             f"Prompt {prompt_org}")

cmvn = load_cmvn(model_config.get('cmvn_path'))
adapter_downsample_rate = model_config.get('adapter_downsample_rate')


hifigan_generator = get_hifigan_model(args.vocoder_path, device, decoder_dim=3584)
spk_encoder = get_speaker_encoder(args.spk_encoder_path, device)
spk_embedding = encode_prompt_wav(spk_encoder, args.prompt_wav_path, device)


def process_wav(wav_path):
    audio_raw, sample_rate = torchaudio.load(wav_path)
    if sample_rate != 16000:
        # resample the data
        resampler = Resample(orig_freq=sample_rate, new_freq=16000)
        audio_raw = resampler(audio_raw)

    if audio_raw.shape[0] > 1:
        # convert to mono
        audio_raw = audio_raw.mean(dim=0, keepdim=True)

    audio_raw = audio_raw[0]
    prompt = prompt_template.format(prompt_org)
    audio_mel = compute_fbank(waveform=audio_raw)
    audio_mel = apply_lfr(inputs=audio_mel, lfr_m=7, lfr_n=6)
    audio_mel = apply_cmvn(audio_mel, cmvn=cmvn)
    audio_length = audio_mel.shape[0]
    audio_length = audio_length // adapter_downsample_rate
    audio_pseudo = torch.full((audio_length,), -1)
    prompt_ids = tokenizer.encode(prompt)
    prompt_length = len(prompt_ids)
    prompt_ids = torch.tensor(prompt_ids, dtype=torch.int64)
    example_ids = torch.cat((audio_pseudo, prompt_ids))  # [audio, prompt]
    example_mask = example_ids.ge(-1)
    
    items = {
        "input_ids": example_ids,
        "attention_mask": example_mask,
        "audio_mel": audio_mel,
        "audio_length": audio_length,
        "prompt_length": prompt_length,
    }
    return items


load_dtype = model_config.get('load_dtype', 'bfloat16')
dtype = torch.float32
if load_dtype == 'float16':
    dtype = torch.float16
elif load_dtype == 'bfloat16':
    dtype = torch.bfloat16
logging.info(f"Input data type: {dtype}")

context_scope = torch.musa.amp.autocast if 'musa' in device else torch.cuda.amp.autocast

with torch.no_grad():
    if args.wav_scp is not None and os.path.exists(args.wav_scp):
        batch_size = args.batch_size
        infer_time = []
        items = parse_key_text(args.wav_scp)
        uttids = list(items.keys())
        num_batches = len(uttids) // batch_size + (0 if len(uttids) % batch_size == 0 else 1)
        for i in range(num_batches):
            try:
                batch_uttids = uttids[i * batch_size:(i + 1) * batch_size]
                batch_wav_paths = [items[uttid] for uttid in batch_uttids]
                samples = []
                for wav_path in batch_wav_paths:
                    samples.append(process_wav(wav_path))
                batch = process_batch(samples, tokenizer=tokenizer)
                for key in batch.keys():
                    batch[key] = batch[key].to(device) if isinstance(batch[key], torch.Tensor) else batch[key]
                with context_scope(dtype=dtype):
                    ss = time.perf_counter()
                    inputs_embeds, attention_mask, kwargs = model.generate(**batch, compute_llm=False)
                    prompt_and_encoding_length = inputs_embeds.shape[1]
                    model_outputs = model.llm.generate(
                        inputs_embeds=inputs_embeds,
                        max_new_tokens=kwargs.get("max_new_tokens", 1000),
                        num_beams=kwargs.get("num_beams", 4),
                        do_sample=True,
                        min_length=kwargs.get("min_length", 1),
                        top_p=0.85,
                        repetition_penalty=kwargs.get("repetition_penalty", 1.0),
                        length_penalty=kwargs.get("length_penalty", 1.0),
                        temperature=kwargs.get("temperature", 1.0),
                        attention_mask=attention_mask,
                        bos_token_id=model.tokenizer.bos_token_id,
                        eos_token_id=model.tokenizer.eos_token_id,
                        pad_token_id=model.tokenizer.pad_token_id,
                    )
                    infer_time.append(time.perf_counter() - ss)
                    logging.info(f"Infer time: {time.perf_counter() - ss}")
                output_text = model.tokenizer.batch_decode(model_outputs, add_special_tokens=False,
                                                           skip_special_tokens=True)
                if hasattr(model.llm.model, "embed_tokens"):
                    teacher_forcing_input_embeds = model.llm.model.embed_tokens(model_outputs)
                    teacher_forcing_input_att_mask = torch.ones((1, teacher_forcing_input_embeds.shape[1]),
                                                                dtype=torch.bool).to(device)
                else:
                    raise NotImplementedError
                inputs_embeds = torch.concat([inputs_embeds, teacher_forcing_input_embeds], dim=-2)
                attention_mask = torch.concat([attention_mask, teacher_forcing_input_att_mask], dim=-1)
                llm_output = model.llm(inputs_embeds=inputs_embeds, attention_mask=attention_mask,
                                       output_hidden_states=True)
                audio_start_index = prompt_and_encoding_length + model_outputs[0].tolist().index(AUDIO_START_TOKEN_INDEX)
                audio_latents = llm_output.hidden_states[-1][:, audio_start_index:-6, :]
                
                for idx, text in enumerate(output_text):
                    logger.info(f"uttid: {batch_uttids[idx]}")
                    audio_file_out_tts = os.path.join(args.output_dir, f"{batch_uttids[idx]}.tts.wav")
                    text_ast = text.split("<|audio_start|>")[0]
                    text_ast = text_ast.replace('\\n', '\n')
                    logger.info(f"AST: {text_ast}")
                    save_wav(hifigan_generator, spk_embedding, audio_latents.float(), audio_file_out_tts)
                    logger.info(f"Finished writing: {audio_file_out_tts}")
            except Exception as e:
                logging.error(e)
        logging.info("Total inference cost")
        logging.info(sum(infer_time))
    elif args.wav_path != '' and os.path.exists(args.wav_path):
        try:
            wav_path = args.wav_path
            items = process_wav(wav_path)
            batch = process_batch([items], tokenizer=tokenizer)
            for key in batch.keys():
                batch[key] = batch[key].to(device) if isinstance(batch[key], torch.Tensor) else batch[key]
            with context_scope(dtype=dtype):
                ss = time.perf_counter()
                inputs_embeds, attention_mask, kwargs = model.generate(**batch, compute_llm=False)
                prompt_and_encoding_length = inputs_embeds.shape[1]
                model_outputs = model.llm.generate(
                    inputs_embeds=inputs_embeds,
                    max_new_tokens=kwargs.get("max_new_tokens", 1000),
                    num_beams=kwargs.get("num_beams", 4),
                    do_sample=True,
                    min_length=kwargs.get("min_length", 1),
                    top_p=0.85,
                    repetition_penalty=kwargs.get("repetition_penalty", 1.0),
                    length_penalty=kwargs.get("length_penalty", 1.0),
                    temperature=kwargs.get("temperature", 1.0),
                    attention_mask=attention_mask,
                    bos_token_id=model.tokenizer.bos_token_id,
                    eos_token_id=model.tokenizer.eos_token_id,
                    pad_token_id=model.tokenizer.pad_token_id,
                )
                logging.info(f"Infer time: {time.perf_counter() - ss}")
            output_text = model.tokenizer.batch_decode(model_outputs, add_special_tokens=False,
                                                       skip_special_tokens=True)
            if hasattr(model.llm.model, "embed_tokens"):
                teacher_forcing_input_embeds = model.llm.model.embed_tokens(model_outputs)
                teacher_forcing_input_att_mask = torch.ones((1, teacher_forcing_input_embeds.shape[1]),
                                                            dtype=torch.bool).to(device)
            else:
                raise NotImplementedError
            inputs_embeds = torch.concat([inputs_embeds, teacher_forcing_input_embeds], dim=-2)
            attention_mask = torch.concat([attention_mask, teacher_forcing_input_att_mask], dim=-1)
            llm_output = model.llm(inputs_embeds=inputs_embeds, attention_mask=attention_mask,
                                   output_hidden_states=True)
            audio_start_index = prompt_and_encoding_length + model_outputs[0].tolist().index(AUDIO_START_TOKEN_INDEX)
            audio_latents = llm_output.hidden_states[-1][:, audio_start_index:-6, :]
            
            for text in output_text:
                uttid = os.path.basename(wav_path).replace(".wav", "")
                audio_file_out_tts = os.path.join(args.output_dir, f"{uttid}.tts.wav")
                text_ast = text.split("<|audio_start|>")[0]
                text_ast = text_ast.replace('\\n', '\n')
                logger.info(f"Text: {text_ast}")
                save_wav(hifigan_generator, spk_embedding, audio_latents.float(), audio_file_out_tts)
                logger.info(f"Finished writing: {audio_file_out_tts}")
        except Exception as e:
            logging.error(e)
    else:
        raise IOError("You should specify --wav_scp or --wav_path as the input")
