# noinspection PyCompatibility
import importlib.util
import os
import sys
import time

import argparse
import chainer
import numpy as np
import six
from PIL import Image

PROJECT_DIR = os.path.join(os.path.dirname(__file__), '..')
waifu2x_path = os.path.join(PROJECT_DIR, "waifu2x-chainer")
sys.path.append(waifu2x_path)


def import_waifu2x_module(name):
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(waifu2x_path, 'lib', ''.join((name, '.py')))
    )
    foo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(foo)
    return foo


iproc = import_waifu2x_module("iproc")
reconstruct = import_waifu2x_module("reconstruct")
srcnn = import_waifu2x_module("srcnn")
utils = import_waifu2x_module("utils")
p = argparse.ArgumentParser()
p.add_argument('--gpu', '-g', type=int, default=-1)
p.add_argument('--input', '-i', default='images/small.png')
p.add_argument('--output_dir', '-o', default='./')
p.add_argument('--extension', '-e', default='png')
p.add_argument('--quality', '-q', type=int, default=None)
p.add_argument('--arch', '-a',
               choices=['VGG7', '0', 'UpConv7', '1',
                        'ResNet10', '2', 'UpResNet10', '3'],
               default='UpResNet10')
p.add_argument('--model_dir', '-d', default=None)
p.add_argument('--method', '-m', choices=['noise', 'scale', 'noise_scale'],
               default='scale')
p.add_argument('--scale_ratio', '-s', type=float, default=2.0)
p.add_argument('--noise_level', '-n', type=int, choices=[0, 1, 2, 3],
               default=1)
p.add_argument('--color', '-c', choices=['y', 'rgb'], default='rgb')
p.add_argument('--tta', '-t', action='store_true')
p.add_argument('--tta_level', '-T', type=int, choices=[2, 4, 8], default=8)
p.add_argument('--batch_size', '-b', type=int, default=16)
p.add_argument('--block_size', '-l', type=int, default=128)
p.add_argument('--width', '-W', type=int, default=0)
p.add_argument('--height', '-H', type=int, default=0)

args, _ = p.parse_known_args()
if args.arch in srcnn.table:
    args.arch = srcnn.table[args.arch]


DEBUG = False


def debug_print(*args, **kwargs):
    if DEBUG:
        six.print_(file=sys.stderr, *args, **kwargs)


def denoise_image(cfg, src, model):
    dst, alpha = split_alpha(src, model)
    debug_print('Level {} denoising...'.format(cfg.noise_level),
               end=' ', flush=True)
    if cfg.tta:
        dst = reconstruct.image_tta(
            dst, model, cfg.tta_level, cfg.block_size, cfg.batch_size)
    else:
        dst = reconstruct.image(dst, model, cfg.block_size, cfg.batch_size)
    if model.inner_scale != 1:
        dst = dst.resize((src.size[0], src.size[1]), Image.LANCZOS)
    debug_print('OK')
    if alpha is not None:
        dst.putalpha(alpha)
    return dst


def upscale_image(cfg, src, scale_model, alpha_model=None):
    dst, alpha = split_alpha(src, scale_model)
    log_scale = np.log2(cfg.scale_ratio)
    for i in range(int(np.ceil(log_scale))):
        debug_print('2.0x upscaling...', end=' ', flush=True)
        model = alpha_model
        if i == 0 or alpha_model is None:
            model = scale_model
        if model.inner_scale == 1:
            dst = iproc.nn_scaling(dst, 2)  # Nearest neighbor 2x scaling
            alpha = iproc.nn_scaling(alpha, 2)  # Nearest neighbor 2x scaling
        if cfg.tta:
            dst = reconstruct.image_tta(
                dst, model, cfg.tta_level, cfg.block_size, cfg.batch_size)
        else:
            dst = reconstruct.image(dst, model, cfg.block_size, cfg.batch_size)
        if alpha_model is None:
            alpha = reconstruct.image(
                alpha, scale_model, cfg.block_size, cfg.batch_size)
        else:
            alpha = reconstruct.image(
                alpha, alpha_model, cfg.block_size, cfg.batch_size)
        debug_print('OK')
    dst_w = int(np.round(src.size[0] * cfg.scale_ratio))
    dst_h = int(np.round(src.size[1] * cfg.scale_ratio))
    if np.round(log_scale % 1.0, 6) != 0 or log_scale <= 0:
        debug_print('Resizing...', end=' ', flush=True)
        dst = dst.resize((dst_w, dst_h), Image.LANCZOS)
        debug_print('OK')
    if alpha is not None:
        if alpha.size[0] != dst_w or alpha.size[1] != dst_h:
            alpha = alpha.resize((dst_w, dst_h), Image.LANCZOS)
        dst.putalpha(alpha)
    return dst


def split_alpha(src, model):
    alpha = None
    if src.mode in ('L', 'RGB', 'P'):
        if isinstance(src.info.get('transparency'), bytes):
            src = src.convert('RGBA')
    rgb = src.convert('RGB')
    if src.mode in ('LA', 'RGBA'):
        debug_print('Splitting alpha channel...', end=' ', flush=True)
        alpha = src.split()[-1]
        rgb = iproc.alpha_make_border(rgb, alpha, model)
        debug_print('OK')
    return rgb, alpha


def load_models(cfg):
    ch = 3 if cfg.color == 'rgb' else 1
    if cfg.model_dir is None:
        model_dir = os.path.join(waifu2x_path, 'models/{}').format(
            cfg.arch.lower())
    else:
        model_dir = cfg.model_dir

    models = {}
    flag = False
    if cfg.method == 'noise_scale':
        model_name = 'anime_style_noise{}_scale_{}.npz'.format(
            cfg.noise_level, cfg.color)
        model_path = os.path.join(model_dir, model_name)
        if os.path.exists(model_path):
            models['noise_scale'] = srcnn.archs[cfg.arch](ch)
            chainer.serializers.load_npz(model_path, models['noise_scale'])
            alpha_model_name = 'anime_style_scale_{}.npz'.format(cfg.color)
            alpha_model_path = os.path.join(model_dir, alpha_model_name)
            models['alpha'] = srcnn.archs[cfg.arch](ch)
            chainer.serializers.load_npz(alpha_model_path, models['alpha'])
        else:
            flag = True
    if cfg.method == 'scale' or flag:
        model_name = 'anime_style_scale_{}.npz'.format(cfg.color)
        model_path = os.path.join(model_dir, model_name)
        models['scale'] = srcnn.archs[cfg.arch](ch)
        chainer.serializers.load_npz(model_path, models['scale'])
    if cfg.method == 'noise' or flag:
        model_name = 'anime_style_noise{}_{}.npz'.format(
            cfg.noise_level, cfg.color)
        model_path = os.path.join(model_dir, model_name)
        if not os.path.exists(model_path):
            model_name = 'anime_style_noise{}_scale_{}.npz'.format(
                cfg.noise_level, cfg.color)
            model_path = os.path.join(model_dir, model_name)
        models['noise'] = srcnn.archs[cfg.arch](ch)
        chainer.serializers.load_npz(model_path, models['noise'])

    if cfg.gpu >= 0:
        chainer.backends.cuda.check_cuda_available()
        chainer.backends.cuda.get_device(cfg.gpu).use()
        for _, model in models.items():
            model.to_gpu()
    return models


# use waifu2x-chainer to process each frame
def process_frame(img: Image, **kwargs) -> Image:
    if kwargs.get("dry_run", False):
        w, h = img.size
        if args.width != 0:
            args.scale_ratio = args.width / w
        if args.height != 0:
            args.scale_ratio = args.height / h

    if 'noise_scale' in models:
        img = upscale_image(
            args, img, models['noise_scale'], models['alpha'])
    else:
        if 'noise' in models:
            img = denoise_image(args, img, models['noise'])
        if 'scale' in models:
            img = upscale_image(args, img, models['scale'])
    return img


models = load_models(args)

if __name__ == '__main__':
    extensions = ['.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp']
    if args.extension not in ['png', 'webp']:
        raise ValueError('{} format is not supported'.format(args.extension))

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    if os.path.isdir(args.input):
        filelist = utils.load_filelist(args.input)
    else:
        filelist = [args.input]

    for path in filelist:
        src = Image.open(path)
        w, h = src.size[:2]
        if args.width != 0:
            args.scale_ratio = args.width / w
        if args.height != 0:
            args.scale_ratio = args.height / h
        outname, ext = os.path.splitext(os.path.basename(path))
        outpath = os.path.join(
            args.output_dir, '{}.{}'.format(outname, args.extension))
        if ext.lower() in extensions:
            outname += '_(tta{})'.format(args.tta_level) if args.tta else '_'
            dst = src.copy()
            start = time.time()
            if 'noise_scale' in models:
                outname += '(noise{}_scale{:.1f}x)'.format(
                    args.noise_level, args.scale_ratio)
                dst = upscale_image(
                    args, dst, models['noise_scale'], models['alpha'])
            else:
                if 'noise' in models:
                    outname += '(noise{})'.format(args.noise_level)
                    dst = denoise_image(args, dst, models['noise'])
                if 'scale' in models:
                    outname += '(scale{:.1f}x)'.format(args.scale_ratio)
                    dst = upscale_image(args, dst, models['scale'])
            print('Elapsed time: {:.6f} sec'.format(time.time() - start))

            outname += '({}_{}).{}'.format(
                args.arch.lower(), args.color, args.extension)
            if os.path.exists(outpath):
                outpath = os.path.join(args.output_dir, outname)

            lossless = args.quality is None
            quality = 100 if lossless else args.quality
            icc_profile = src.info.get('icc_profile')
            icc_profile = "" if icc_profile is None else icc_profile
            dst.convert(src.mode).save(
                outpath, quality=quality, lossless=lossless,
                icc_profile=icc_profile)
            six.print_('Saved as \'{}\''.format(outpath))
