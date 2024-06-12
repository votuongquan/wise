import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision.models as models
import torchvision.transforms as transforms
import numpy as np
import os
import math
import PIL
from time import time
from argparse import ArgumentParser

# This code is adapted from https://github.com/futscdav/strotss
# Copyright by David Futschik
# with only minor changes


class Vgg16_Extractor(nn.Module):
    def __init__(self, space):
        super().__init__()
        self.vgg_layers = models.vgg16(weights='DEFAULT').features

        for param in self.parameters():
            param.requires_grad = False
        self.capture_layers = [1, 3, 6, 8, 11, 13, 15, 22, 29]
        self.space = space

    def forward_base(self, x):
        feat = [x]
        for i in range(len(self.vgg_layers)):
            x = self.vgg_layers[i](x)
            if i in self.capture_layers: feat.append(x)
        return feat

    def forward(self, x):
        if self.space != 'vgg':
            x = (x + 1.) / 2.
            x = x - (torch.Tensor([0.485, 0.456, 0.406]).to(x.device).view(1, -1, 1, 1))
            x = x / (torch.Tensor([0.229, 0.224, 0.225]).to(x.device).view(1, -1, 1, 1))
        feat = self.forward_base(x)
        return feat

    def forward_samples_hypercolumn(self, X, samps=100):
        feat = self.forward(X)

        xx, xy = np.meshgrid(np.arange(X.shape[2]), np.arange(X.shape[3]))
        xx = np.expand_dims(xx.flatten(), 1)
        xy = np.expand_dims(xy.flatten(), 1)
        xc = np.concatenate([xx, xy], 1)

        samples = min(samps, xc.shape[0])

        np.random.shuffle(xc)
        xx = xc[:samples, 0]
        yy = xc[:samples, 1]

        feat_samples = []
        for i in range(len(feat)):

            layer_feat = feat[i]

            # hack to detect lower resolution
            if i > 0 and feat[i].size(2) < feat[i - 1].size(2):
                xx = xx / 2.0
                yy = yy / 2.0

            xx = np.clip(xx, 0, layer_feat.shape[2] - 1).astype(np.int32)
            yy = np.clip(yy, 0, layer_feat.shape[3] - 1).astype(np.int32)

            features = layer_feat[:, :, xx[range(samples)], yy[range(samples)]]
            feat_samples.append(features.clone().detach())

        feat = torch.cat(feat_samples, 1)
        return feat


# Tensor and PIL utils

def pil_loader(path):
    with open(path, 'rb') as f:
        img = PIL.Image.open(f)
        return img.convert('RGB')


def tensor_resample(tensor, dst_size, mode='bilinear'):
    return F.interpolate(tensor, dst_size, mode=mode, align_corners=False)


def pil_resize_short_edge_to(pil, trg_size):
    short_w = pil.width < pil.height
    ar_resized_short = (trg_size / pil.width) if short_w else (trg_size / pil.height)
    resized = pil.resize((int(pil.width * ar_resized_short), int(pil.height * ar_resized_short)), PIL.Image.BICUBIC)
    return resized


def pil_resize_long_edge_to(pil, trg_size):
    short_w = pil.width < pil.height
    ar_resized_long = (trg_size / pil.height) if short_w else (trg_size / pil.width)
    resized = pil.resize((int(pil.width * ar_resized_long), int(pil.height * ar_resized_long)), PIL.Image.BICUBIC)
    return resized


def np_to_pil(npy):
    return PIL.Image.fromarray(npy.astype(np.uint8))


def pil_to_np(pil):
    return np.array(pil)


def tensor_to_np(tensor, cut_dim_to_3=True):
    if len(tensor.shape) == 4:
        if cut_dim_to_3:
            tensor = tensor[0]
        else:
            return tensor.data.cpu().numpy().transpose((0, 2, 3, 1))
    return tensor.data.cpu().numpy().transpose((1, 2, 0))


def np_to_tensor(npy, space):
    if space == 'vgg':
        return np_to_tensor_correct(npy)
    return (torch.Tensor(npy.astype(np.float64) / 127.5) - 1.0).permute((2, 0, 1)).unsqueeze(0)


def np_to_tensor_correct(npy):
    pil = np_to_pil(npy)
    transform = transforms.Compose([transforms.ToTensor(),
                                    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
                                    ])
    return transform(pil).unsqueeze(0)


# Laplacian Pyramid

def laplacian(x):
    # x - upsample(downsample(x))
    return x - tensor_resample(tensor_resample(x, [x.shape[2] // 2, x.shape[3] // 2]), [x.shape[2], x.shape[3]])


def make_laplace_pyramid(x, levels):
    pyramid = []
    current = x
    for i in range(levels):
        pyramid.append(laplacian(current))
        current = tensor_resample(current, (max(current.shape[2] // 2, 1), max(current.shape[3] // 2, 1)))
    pyramid.append(current)
    return pyramid


def fold_laplace_pyramid(pyramid):
    current = pyramid[-1]
    for i in range(len(pyramid) - 2, -1, -1):  # iterate from len-2 to 0
        up_h, up_w = pyramid[i].shape[2], pyramid[i].shape[3]
        current = pyramid[i] + tensor_resample(current, (up_h, up_w))
    return current


def sample_indices(feat_content, feat_style):
    indices = None
    const = 128 ** 2  # 32k or so
    feat_dims = feat_style.shape[1]
    big_size = feat_content.shape[2] * feat_content.shape[3]  # num feaxels

    stride_x = int(max(math.floor(math.sqrt(big_size // const)), 1))
    offset_x = np.random.randint(stride_x)
    stride_y = int(max(math.ceil(math.sqrt(big_size // const)), 1))
    offset_y = np.random.randint(stride_y)
    xx, xy = np.meshgrid(np.arange(feat_content.shape[2])[offset_x::stride_x],
                         np.arange(feat_content.shape[3])[offset_y::stride_y])

    xx = xx.flatten()
    xy = xy.flatten()
    return xx, xy


def spatial_feature_extract(feat_result, feat_content, xx, xy):
    l2, l3 = [], []
    device = feat_result[0].device

    # for each extracted layer
    for i in range(len(feat_result)):
        fr = feat_result[i]
        fc = feat_content[i]

        # hack to detect reduced scale
        if i > 0 and feat_result[i - 1].size(2) > feat_result[i].size(2):
            xx = xx / 2.0
            xy = xy / 2.0

        # go back to ints and get residual
        xxm = np.floor(xx).astype(np.float32)
        xxr = xx - xxm

        xym = np.floor(xy).astype(np.float32)
        xyr = xy - xym

        # do bilinear resample
        w00 = torch.from_numpy((1. - xxr) * (1. - xyr)).float().view(1, 1, -1, 1).to(device)
        w01 = torch.from_numpy((1. - xxr) * xyr).float().view(1, 1, -1, 1).to(device)
        w10 = torch.from_numpy(xxr * (1. - xyr)).float().view(1, 1, -1, 1).to(device)
        w11 = torch.from_numpy(xxr * xyr).float().view(1, 1, -1, 1).to(device)

        xxm = np.clip(xxm.astype(np.int32), 0, fr.size(2) - 1)
        xym = np.clip(xym.astype(np.int32), 0, fr.size(3) - 1)

        s00 = xxm * fr.size(3) + xym
        s01 = xxm * fr.size(3) + np.clip(xym + 1, 0, fr.size(3) - 1)
        s10 = np.clip(xxm + 1, 0, fr.size(2) - 1) * fr.size(3) + (xym)
        s11 = np.clip(xxm + 1, 0, fr.size(2) - 1) * fr.size(3) + np.clip(xym + 1, 0, fr.size(3) - 1)

        fr = fr.view(1, fr.size(1), fr.size(2) * fr.size(3), 1)
        fr = fr[:, :, s00, :].mul_(w00).add_(fr[:, :, s01, :].mul_(w01)).add_(fr[:, :, s10, :].mul_(w10)).add_(
            fr[:, :, s11, :].mul_(w11))

        fc = fc.view(1, fc.size(1), fc.size(2) * fc.size(3), 1)
        fc = fc[:, :, s00, :].mul_(w00).add_(fc[:, :, s01, :].mul_(w01)).add_(fc[:, :, s10, :].mul_(w10)).add_(
            fc[:, :, s11, :].mul_(w11))

        l2.append(fr)
        l3.append(fc)

    x_st = torch.cat([li.contiguous() for li in l2], 1)
    c_st = torch.cat([li.contiguous() for li in l3], 1)

    xx = torch.from_numpy(xx).view(1, 1, x_st.size(2), 1).float().to(device)
    yy = torch.from_numpy(xy).view(1, 1, x_st.size(2), 1).float().to(device)

    x_st = torch.cat([x_st, xx, yy], 1)
    c_st = torch.cat([c_st, xx, yy], 1)
    return x_st, c_st


def pairwise_distances_cos(x, y):
    x_norm = torch.sqrt((x ** 2).sum(1).view(-1, 1))
    y_t = torch.transpose(y, 0, 1)
    y_norm = torch.sqrt((y ** 2).sum(1).view(1, -1))
    dist = 1. - torch.mm(x, y_t) / x_norm / y_norm
    return dist


def pairwise_distances_sq_l2(x, y):
    x_norm = (x ** 2).sum(1).view(-1, 1)
    y_t = torch.transpose(y, 0, 1)
    y_norm = (y ** 2).sum(1).view(1, -1)
    dist = x_norm + y_norm - 2.0 * torch.mm(x, y_t)
    return torch.clamp(dist, 1e-5, 1e5) / x.size(1)


def distmat(x, y, cos_d=True):
    if cos_d:
        M = pairwise_distances_cos(x, y)
    else:
        M = torch.sqrt(pairwise_distances_sq_l2(x, y))
    return M


def content_loss(feat_result, feat_content):
    d = feat_result.size(1)

    X = feat_result.transpose(0, 1).contiguous().view(d, -1).transpose(0, 1)
    Y = feat_content.transpose(0, 1).contiguous().view(d, -1).transpose(0, 1)

    Y = Y[:, :-2]
    X = X[:, :-2]
    # X = X.t()
    # Y = Y.t()

    Mx = distmat(X, X)
    Mx = Mx  # /Mx.sum(0, keepdim=True)

    My = distmat(Y, Y)
    My = My  # /My.sum(0, keepdim=True)

    d = torch.abs(Mx - My).mean()  # * X.shape[0]
    return d


def rgb_to_yuv(rgb):
    C = torch.Tensor(
        [[0.577350, 0.577350, 0.577350], [-0.577350, 0.788675, -0.211325], [-0.577350, -0.211325, 0.788675]]).to(
        rgb.device)
    yuv = torch.mm(C, rgb)
    return yuv


def style_loss(X, Y, cos_d=True):
    d = X.shape[1]

    if d == 3:
        X = rgb_to_yuv(X.transpose(0, 1).contiguous().view(d, -1)).transpose(0, 1)
        Y = rgb_to_yuv(Y.transpose(0, 1).contiguous().view(d, -1)).transpose(0, 1)
    else:
        X = X.transpose(0, 1).contiguous().view(d, -1).transpose(0, 1)
        Y = Y.transpose(0, 1).contiguous().view(d, -1).transpose(0, 1)

    # Relaxed EMD
    CX_M = distmat(X, Y, cos_d=True)

    if d == 3: CX_M = CX_M + distmat(X, Y, cos_d=False)

    m1, m1_inds = CX_M.min(1)
    m2, m2_inds = CX_M.min(0)

    remd = torch.max(m1.mean(), m2.mean())

    return remd


def moment_loss(X, Y, moments=[1, 2]):
    loss = 0.
    X = X.squeeze().t()
    Y = Y.squeeze().t()

    mu_x = torch.mean(X, 0, keepdim=True)
    mu_y = torch.mean(Y, 0, keepdim=True)
    mu_d = torch.abs(mu_x - mu_y).mean()

    if 1 in moments:
        # print(mu_x.shape)
        loss = loss + mu_d

    if 2 in moments:
        X_c = X - mu_x
        Y_c = Y - mu_y
        X_cov = torch.mm(X_c.t(), X_c) / (X.shape[0] - 1)
        Y_cov = torch.mm(Y_c.t(), Y_c) / (Y.shape[0] - 1)

        # print(X_cov.shape)
        # exit(1)

        D_cov = torch.abs(X_cov - Y_cov).mean()
        loss = loss + D_cov

    return loss


def calculate_loss(feat_result, feat_content, feat_style, indices, content_weight, moment_weight=1.0):
    # spatial feature extract
    num_locations = 1024
    spatial_result, spatial_content = spatial_feature_extract(feat_result, feat_content, indices[0][:num_locations],
                                                              indices[1][:num_locations])
    loss_content = content_loss(spatial_result, spatial_content)

    d = feat_style.shape[1]
    spatial_style = feat_style.view(1, d, -1, 1)
    feat_max = 3 + 2 * 64 + 128 * 2 + 256 * 3 + 512 * 2  # (sum of all extracted channels)

    loss_remd = style_loss(spatial_result[:, :feat_max, :, :], spatial_style[:, :feat_max, :, :])

    loss_moment = moment_loss(spatial_result[:, :-2, :, :], spatial_style, moments=[1, 2])  # -2 is so that it can fit?
    # palette matching
    content_weight_frac = 1. / max(content_weight, 1.)
    loss_moment += content_weight_frac * style_loss(spatial_result[:, :3, :, :], spatial_style[:, :3, :, :])

    loss_style = loss_remd + moment_weight * loss_moment
    # print(f'Style: {loss_style.item():.3f}, Content: {loss_content.item():.3f}')

    style_weight = 1.0 + moment_weight
    loss_total = (content_weight * loss_content + loss_style) / (content_weight + style_weight)
    return loss_total


def optimize(result, content, style, scale, content_weight, lr, extractor):
    # torch.autograd.set_detect_anomaly(True)
    result_pyramid = make_laplace_pyramid(result, 5)
    result_pyramid = [l.data.requires_grad_() for l in result_pyramid]

    opt_iter = 200
    # if scale == 1:
    #     opt_iter = 800

    # use rmsprop
    optimizer = optim.RMSprop(result_pyramid, lr=lr)

    # extract features for content
    feat_content = extractor(content)  #

    stylized = fold_laplace_pyramid(result_pyramid)
    # let's ignore the regions for now
    # some inner loop that extracts samples
    feat_style = None
    for i in range(5):
        with torch.no_grad():
            # r is region of interest (mask)
            feat_e = extractor.forward_samples_hypercolumn(style, samps=1000)
            feat_style = feat_e if feat_style is None else torch.cat((feat_style, feat_e), dim=2)
    # feat_style.requires_grad_(False)

    # init indices to optimize over
    xx, xy = sample_indices(feat_content[0], feat_style)  # 0 to sample over first layer extracted
    for it in range(opt_iter):
        optimizer.zero_grad()

        stylized = fold_laplace_pyramid(result_pyramid)
        # original code has resample here, seems pointless with uniform shuffle
        # ...
        # also shuffle them every y iter
        if it % 1 == 0 and it != 0:
            np.random.shuffle(xx)
            np.random.shuffle(xy)
        feat_result = extractor(stylized)

        loss = calculate_loss(feat_result, feat_content, feat_style, [xx, xy], content_weight)
        loss.backward()
        optimizer.step()
    return stylized


def strotss(content_pil, style_pil, content_weight=1.0 * 16.0, device='cuda:0', space='uniform'):
    content_np = pil_to_np(content_pil)
    style_np = pil_to_np(style_pil)
    content_full = np_to_tensor(content_np, space).to(device)
    style_full = np_to_tensor(style_np, space).to(device)

    lr = 2e-3
    extractor = Vgg16_Extractor(space=space).to(device)

    scale_last = max(content_full.shape[2], content_full.shape[3])
    scales = []
    for scale in range(10):
        divisor = 2 ** scale
        if min(content_pil.width, content_pil.height) // divisor >= 33:
            scales.insert(0, divisor)

    for scale in scales:
        # rescale content to current scale
        content = tensor_resample(content_full, [content_full.shape[2] // scale, content_full.shape[3] // scale])
        style = tensor_resample(style_full, [style_full.shape[2] // scale, style_full.shape[3] // scale])
        print(f'Optimizing at resoluton [{content.shape[2]}, {content.shape[3]}]')

        # upsample or initialize the result
        if scale == scales[0]:
            # first
            result = laplacian(content) + style.mean(2, keepdim=True).mean(3, keepdim=True)
        elif scale == scales[-1]:
            # last 
            result = tensor_resample(result, [content.shape[2], content.shape[3]])
            lr = 1e-3
        else:
            result = tensor_resample(result, [content.shape[2], content.shape[3]]) + laplacian(content)

        # do the optimization on this scale
        result = optimize(result, content, style, scale, content_weight=content_weight, lr=lr, extractor=extractor)

        # next scale lower weight
        content_weight /= 2.0

    clow = -1.0 if space == 'uniform' else -1.7
    chigh = 1.0 if space == 'uniform' else 1.7
    result_image = tensor_to_np(
        tensor_resample(torch.clamp(result, clow, chigh), [content_full.shape[2], content_full.shape[3]]))  #
    # renormalize image
    result_image -= result_image.min()
    result_image /= result_image.max()
    return np_to_pil(result_image * 255.)


def execute_style_transfer(content_path, style_path, resize_to, weight=1.0, device="cuda:0"):
    content_pil, style_pil = pil_loader(content_path), pil_loader(style_path)
    content_weight = weight * 16.0

    return strotss(pil_resize_long_edge_to(content_pil, resize_to),
                   pil_resize_long_edge_to(style_pil, resize_to), content_weight, device, "uniform")


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--content", type=str, default="flower.jpg")
    parser.add_argument("--style", type=str, default="wc_flower2.jpg")
    parser.add_argument("--weight", type=float, default=1.0)
    parser.add_argument("--output", type=str, default="strotss.png")
    parser.add_argument("--device", type=str, default="cuda:0")
    # uniform ospace = optimization done in [-1, 1], else imagenet normalized space
    parser.add_argument("--ospace", type=str, default="uniform", choices=["uniform", "vgg"])
    parser.add_argument("--resize_to", type=int, default=1024)
    args = parser.parse_args()

    # make 256 the smallest possible long side, will still fail if short side is <
    if args.resize_to < 2 ** 8:
        print("Resulution too low.")
        exit(1)

    content_pil, style_pil = pil_loader(args.content), pil_loader(args.style)
    content_weight = args.weight * 16.0

    device = args.device

    start = time()
    result = strotss(pil_resize_long_edge_to(content_pil, args.resize_to),
                     pil_resize_long_edge_to(style_pil, args.resize_to), content_weight, device, args.ospace)
    result.save(args.output)
    print(f'Done in {time() - start:.3f}s')
