"""
To run on a single GPU, example:
$ python train.py --batch_size=32 --compile=False
"""
import os
import time
import math
import pickle
from contextlib import nullcontext
import numpy as np
import torch
from model import GPTConfig, GPT
import logging  # Better than printing because it is saved in a log file as well

import hydra


def create_model_from_scratch(model_args, meta_vocab_size):
    """Creates a new model to train"""
    logging.info("Initializing a new model from scratch")
    # determine the vocab size we'll use for from-scratch training
    assert(meta_vocab_size is not None)
    model_args['vocab_size'] = meta_vocab_size
    gptconf = GPTConfig(**model_args)
    return GPT(gptconf)


def load_model(model_args, config):
    """Loads an old model"""
    logging.info(f"Resuming training from {config['out_dir']}")
    # resume training from a checkpoint.
    ckpt_path = os.path.join(config['out_dir'], 'ckpt.pt')
    checkpoint = torch.load(ckpt_path, map_location=config['device'])
    checkpoint_model_args = checkpoint['model_args']
    # force these config attributes to be equal otherwise we can't even resume training
    # the rest of the attributes (e.g. dropout) can stay as desired from command line
    for k in ['n_layer', 'n_head', 'n_embd', 'block_size', 'bias', 'vocab_size']:
        model_args[k] = checkpoint_model_args[k]
    # create the model
    gptconf = GPTConfig(**model_args)
    model = GPT(gptconf)
    state_dict = checkpoint['model']
    # fix the keys of the state dictionary :(
    # honestly no idea how checkpoints sometimes get this prefix, have to debug more
    unwanted_prefix = '_orig_mod.'
    for k,v in list(state_dict.items()):
        if k.startswith(unwanted_prefix):
            state_dict[k[len(unwanted_prefix):]] = state_dict.pop(k)
    model.load_state_dict(state_dict)

    iter_num = checkpoint['iter_num']
    best_val_loss = checkpoint['best_val_loss']

    return model, checkpoint, iter_num, best_val_loss

# helps estimate an arbitrarily accurate loss over either split using many batches
@torch.no_grad()
def estimate_loss(model, context, eval_iters):
    out = {}
    model.eval()
    for split in ['train', 'val']:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            with context:
                _, loss = model(X, Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out


# learning rate decay scheduler (cosine with warmup) # Ted: Dynamic learning rate IMO.
def get_lr(it, learning_rate, warmup_iters, lr_decay_iters, min_lr):
    # 1) linear warmup for warmup_iters steps
    if it < warmup_iters:
        return learning_rate * it / warmup_iters
    # 2) if it > lr_decay_iters, return min learning rate
    if it > lr_decay_iters:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio)) # coeff ranges 0..1
    return min_lr + coeff * (learning_rate - min_lr)


def get_batch(data, device, block_size, batch_size):
    """Get a batch from the inputted data.
    This is modified to simply take in the array.
    """
    ix = torch.randint(len(data) - block_size, (batch_size,))
    x = torch.stack([torch.from_numpy((data[i:i+block_size]).astype(np.int64)) for i in ix]) # Ted: Stack into a batch tensor.
    y = torch.stack([torch.from_numpy((data[i+1:i+1+block_size]).astype(np.int64)) for i in ix])
    if 'cuda' in device:
        # pin arrays x,y, which allows us to move them to GPU asynchronously (non_blocking=True)
        x, y = x.pin_memory().to(device, non_blocking=True), y.pin_memory().to(device, non_blocking=True)
    else:
        x, y = x.to(device), y.to(device) # Ted: Move a tensor to device.
    return x, y



@hydra.main(version_base=None, config_path="config", config_name="config")
def train_from_scratch(config):
    """hydra decorated functions can only take in one parameter.
    This is one way to get around this."""
    return train(config, True)


@hydra.main(version_base=None, config_path="config", config_name="config")
def train_resume(config):
    """hydra decorated functions can only take in one parameter.
    This is one way to get around this."""
    return train(config, False)



def train(config, start_from_scratch):
    """Trains a model on the current configurations.
    config: The dictionary of configurations.
    start_from_scratch: If False, load a previous checkpoint. Otherwise, start from scratch.
    output: The model (avoids needing to get the model from a file in Agent.py)
    """
    # various inits, derived attributes, I/O setup
    # We are running on a single gpu, and one process.
    gradient_accumulation_steps = 8 * config['gradient_accumulation_steps']  # simulate 8 gpus
    os.makedirs(config['out_dir'], exist_ok=True)
    torch.manual_seed(1337 + config['seed_offset'])
    torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
    torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn

    device_type = 'cuda' if 'cuda' in config['device'] else 'cpu' # for later use in torch.autocast
    # note: float16 data type will automatically use a GradScaler
    ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[config['dtype']]
    context =  nullcontext() if device_type=='cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype)

    # poor man's data loader
    data_dir = os.path.join('data', config['dataset'])
    train_data = np.memmap(os.path.join(data_dir, 'train.bin'), dtype=np.uint16, mode='r') # Ted: Allow to read large file without needing to fit entire file into physical memory (i.e. RAM).
    val_data = np.memmap(os.path.join(data_dir, 'val.bin'), dtype=np.uint16, mode='r')

    # init these up here, can override if init_from_scratch is False (i.e. from a checkpoint)
    iter_num = 0
    best_val_loss = 1e9

    # attempt to derive vocab_size from the dataset
    meta_path = os.path.join(data_dir, 'meta.pkl')
    meta_vocab_size = None
    if os.path.exists(meta_path):
        with open(meta_path, 'rb') as f:
            meta = pickle.load(f)
        meta_vocab_size = meta['vocab_size']
        print(f"found vocab_size = {meta_vocab_size} (inside {meta_path})")

    # optimizer
    optimizer = model.configure_optimizers(
        config['weight_decay'], config['learning_rate'], (config['beta1'], config['beta2']), 'cuda' if 'cuda' in config['device'] else 'cpu'
    )

    # model init
    model_args = dict(n_layer=config['n_layer'], n_head=config['n_head'], n_embd=config['n_embd'], block_size=config['block_size'],
                    bias=config['bias'], vocab_size=None, dropout=config['dropout'])
    if start_from_scratch:
        model = create_model_from_scratch(model_args, meta_vocab_size)
    else:
        model, checkpoint, iter_num, best_val_loss = load_model(model_args, config)
        optimizer.load_state_dict(checkpoint)
    # crop down the model block size if desired, using model surgery
    if config['block_size'] < model.config.block_size:
        model.crop_block_size(config['block_size'])
        model_args['block_size'] = config['block_size'] # so that the checkpoint will have the right value
    model.to(config['device'])

    # compile the model
    if config['compile']:
        print("compiling the model... (takes a ~minute)")
        unoptimized_model = model  # Mark: Can we remove this line?
        model = torch.compile(model) # requires PyTorch 2.0

    # initialize a GradScaler. If enabled=False scaler is a no-op
    scaler = torch.cuda.amp.GradScaler(enabled=(config['dtype'] == 'float16')) # Ted: To prevent numerical instability.

    # training loop
    X, Y = get_batch(train_data, config['device'], config['block_size'], config['batch-size']) # fetch the very first batch
    t0 = time.time()
    local_iter_num = 0 # number of iterations in the lifetime of this process
    raw_model = model
    running_mfu = -1.0
    while True:
        # determine and set the learning rate for this iteration
        lr = get_lr(
            iter_num, config['learning_rate'], config['warmup_iters'], config['lr_decay_iters'], config['min_lr']
        ) if config['decay_lr'] else config['learning_rate']
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        # evaluate the loss on train/val sets and write checkpoints
        if iter_num % config['eval_interval'] == 0:
            losses = estimate_loss(model, context, config['eval_iters'])
            print(f"step {iter_num}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")
            if losses['val'] < best_val_loss or config['always_save_checkpoint']:
                best_val_loss = losses['val']
                if iter_num > 0:
                    checkpoint = {
                        'model': raw_model.state_dict(),
                        'optimizer': optimizer.state_dict(),
                        'model_args': model_args,
                        'iter_num': iter_num,
                        'best_val_loss': best_val_loss,
                        'config': config,
                    }
                    print(f"saving checkpoint to {config['out_dir']}")
                    torch.save(checkpoint, os.path.join(config['out_dir'], 'ckpt.pt')) # Ted: ".pt" file is PyTorch's serialized file of a model object.
        if iter_num == 0 and config['eval_only']:
            break
        # forward backward update, with optional gradient accumulation to simulate larger batch size
        # and using the GradScaler if data type is float16
        for micro_step in range(gradient_accumulation_steps):
            with context:
                logits, loss = model(X, Y)
            # immediately async prefetch next batch while model is doing the forward pass on the GPU
            X, Y = get_batch(train_data, config['device'], config['block_size'], config['batch_size'])
            # backward pass, with gradient scaling if training in fp16
            scaler.scale(loss).backward()
        # clip the gradient
        if config['grad_clip'] != 0.0:
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), config['grad_clip'])
        # step the optimizer and scaler if training in fp16
        scaler.step(optimizer)
        scaler.update()
        # flush the gradients as soon as we can, no need for this memory anymore
        optimizer.zero_grad(set_to_none=True)
        # timing and logging
        t1 = time.time()
        dt = t1 - t0
        t0 = t1
        if iter_num % config['log_interval'] == 0: # Ted: <log_interval> is e.g. print curr iteration report to command line.
            lossf = loss.item() # loss as float. note: this is a CPU-GPU sync point
            if local_iter_num >= 5: # let the training loop settle a bit
                mfu = raw_model.estimate_mfu(config['batch_size'] * gradient_accumulation_steps, dt)
                running_mfu = mfu if running_mfu == -1.0 else 0.9*running_mfu + 0.1*mfu
            print(f"iter {iter_num}: loss {lossf:.4f}, time {dt*1000:.2f}ms, mfu {running_mfu*100:.2f}%")
        iter_num += 1
        local_iter_num += 1
        # termination conditions
        if iter_num > config['max_iters']:
            break

    return model


if __name__ == '__main__':
    train()