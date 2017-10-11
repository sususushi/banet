# coding: utf-8

'''
Boundary-aware video captioning
'''

import math
import random
from builtins import range

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
from torch.autograd import Variable, Function
import torchvision.models as models

from args import resnet_checkpoint, vgg_checkpoint


class VisualEncoder(nn.Module):

    # 使用ResNet50作为视觉特征提取器
    def __init__(self):
        super(VisualEncoder, self).__init__()
        self.resnet = models.resnet50()
        self.resnet.load_state_dict(torch.load(resnet_checkpoint))
        del self.resnet.fc

    def forward(self, x):
        x = self.resnet.conv1(x)
        x = self.resnet.bn1(x)
        x = self.resnet.relu(x)
        x = self.resnet.maxpool(x)

        x = self.resnet.layer1(x)
        x = self.resnet.layer2(x)
        x = self.resnet.layer3(x)
        x = self.resnet.layer4(x)

        x = self.resnet.avgpool(x)
        x = x.view(x.size(0), -1)

        return x

    # 使用VGG作为视觉特征提取器
    # def __init__(self):
    #     super(VisualEncoder, self).__init__()
    #     self.vgg = models.vgg16()
    #     self.vgg.load_state_dict(torch.load(vgg_checkpoint))
    #     # 把VGG的最后一个fc层（其之前的ReLU层要保留）剔除掉
    #     self.vgg.classifier = nn.Sequential(*list(self.vgg.classifier.children())[:-1])

    # def forward(self, images):
    #     return self.vgg(images)


class BinaryGate(Function):
    '''
    二值门单元
    forward中的二值门单元分为train和eval两种：
    train: 阈值为[0,1]内均匀分布的随机采样值随机的二值神经元，
    eval: 固定阈值为0.5的二值神经元
    backward中的二值门单元的导函数用identity函数
    '''

    @staticmethod
    def forward(ctx, input, training=False, inplace=False):
        if inplace:
            output = input
        else:
            output = input.clone()
        ctx.thrs = random.uniform(0, 1) if training else 0.5
        output[output > ctx.thrs] = 1
        output[output <= ctx.thrs] = 0
        # print(input)
        # print(ctx.thrs)
        return output

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output, None, None


class BoundaryDetector(nn.Module):
    '''
    Boundary Detector，边界检测模块
    '''

    def __init__(self, i_features, h_features, s_features, inplace=False):
        super(BoundaryDetector, self).__init__()
        self.inplace = inplace
        self.Wsi = Parameter(torch.Tensor(s_features, i_features))
        self.Wsh = Parameter(torch.Tensor(s_features, h_features))
        self.bias = Parameter(torch.Tensor(s_features))
        self.vs = Parameter(torch.Tensor(1, s_features))
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.Wsi.size(1))
        self.Wsi.data.uniform_(-stdv, stdv)
        self.Wsh.data.uniform_(-stdv, stdv)
        self.bias.data.uniform_(-stdv, stdv)
        self.vs.data.uniform_(-stdv, stdv)

    def forward(self, x, h):
        z = F.linear(x, self.Wsi) + F.linear(h, self.Wsh) + self.bias
        z = F.sigmoid(F.linear(z, self.vs))
        return BinaryGate.apply(z, self.training, self.inplace)

    def __repr__(self):
        return self.__class__.__name__


class Encoder(nn.Module):
    '''
    Hierarchical Boundart-Aware视频编码器
    '''

    def __init__(self, frame_size, projected_size, mid_size, hidden_size, max_frames):
        '''
        frame_size: 视频帧的特征大小，2048维
        projected_size: 特征的投影维度
        mid_size: BD单元的中间表达维度
        hidden_size: LSTM的隐藏单元个数（隐层表示的维度）
        num_frames: 视觉特征的序列长度
        '''
        super(Encoder, self).__init__()

        self.frame_size = frame_size
        self.projected_size = projected_size
        self.hidden_size = hidden_size
        self.max_frames = max_frames

        # frame_embed用来把视觉特征降维
        self.frame_embed = nn.Linear(frame_size, projected_size)
        self.frame_drop = nn.Dropout(p=0.5)

        # lstm1_cell是低层的视频序列编码单元
        self.lstm1_cell = nn.LSTMCell(projected_size, hidden_size)
        self.lstm1_drop = nn.Dropout(p=0.5)

        # bd是一个边界检测单元
        self.bd = BoundaryDetector(projected_size, hidden_size, mid_size)

        # lstm2_cell是高层的视频序列编码单元
        self.lstm2_cell = nn.LSTMCell(hidden_size, hidden_size, bias=False)
        self.lstm2_drop = nn.Dropout(p=0.5)

    def _init_lstm_state(self, d):
        bsz = d.size(0)
        return Variable(d.data.new(bsz, self.hidden_size).zero_()), \
            Variable(d.data.new(bsz, self.hidden_size).zero_())

    def forward(self, video_feats):
        '''
        用Hierarchical Boundary-Aware Neural Encoder对视频进行编码
        '''
        batch_size = len(video_feats)
        # 初始化LSTM状态
        lstm1_h, lstm1_c = self._init_lstm_state(video_feats)
        lstm2_h, lstm2_c = self._init_lstm_state(video_feats)

        v = video_feats.view(-1, self.frame_size)
        v = self.frame_embed(v)
        v = self.frame_drop(v)
        v = v.view(batch_size, -1, self.projected_size)

        for i in range(self.max_frames):
            s = self.bd(v[:, i, :], lstm1_h)
            # print(sum(s.data)[0])
            lstm1_h, lstm1_c = self.lstm1_cell(v[:, i, :], (lstm1_h, lstm1_c))
            lstm1_h = self.lstm1_drop(lstm1_h)

            lstm2_input = lstm1_h * s
            lstm2_h, lstm2_c = self.lstm2_cell(lstm2_input, (lstm2_h, lstm2_c))
            lstm2_h = self.lstm2_drop(lstm2_h)

            lstm1_h = lstm1_h * (1 - s)
            lstm1_c = lstm1_c * (1 - s)

        return lstm1_h


class Decoder(nn.Module):
    '''
    视频内容解码器
    '''
    def __init__(self, encoded_size, projected_size, hidden_size,
                 max_words, vocab):
        super(Decoder, self).__init__()
        self.encoded_size = encoded_size
        self.projected_size = projected_size
        self.hidden_size = hidden_size
        self.max_words = max_words
        self.vocab = vocab
        self.vocab_size = len(vocab)

        self.word_embed = nn.Embedding(self.vocab_size, projected_size)
        self.word_drop = nn.Dropout(p=0.5)
        # 文章中的GRU是有三个输入的，除了输入GRU的上一个隐层状态
        # 还需要输入视频特征和单词特征这两个维度的特征
        # 但是标准的GRU只接受两个输入
        # 因此在GRU之外先使用两个全连接层把两个维度的特征合并成一维
        self.v2m = nn.Linear(encoded_size, projected_size)
        self.w2m = nn.Linear(projected_size, projected_size)
        self.gru_cell = nn.GRUCell(projected_size, hidden_size)
        self.gru_drop = nn.Dropout(p=0.5)
        self.word_restore = nn.Linear(hidden_size, self.vocab_size)

    def _init_gru_state(self, d):
        bsz = d.size(0)
        return Variable(d.data.new(bsz, self.hidden_size).zero_())

    def forward(self, video_encoded, captions, teacher_forcing_ratio=0.5):
        batch_size = len(video_encoded)
        # 根据是否传入caption判断是否是推断模式
        infer = True if captions is None else False
        # 初始化GRU状态
        gru_h = self._init_gru_state(video_encoded)

        outputs = []
        # 先送一个<start>标记
        word_id = self.vocab('<start>')
        word = Variable(video_encoded.data.new(batch_size, 1).long().fill_(word_id))
        word = self.word_embed(word).squeeze(1)
        word = self.word_drop(word)

        vm = self.v2m(video_encoded)
        for i in range(self.max_words):
            if not infer and captions[:, i].data.sum() == 0:
                # <pad>的id是0，如果所有的word id都是0，
                # 意味着所有的句子都结束了，没有必要再算了
                break
            wm = self.w2m(word)
            m = vm + wm
            gru_h = self.gru_cell(m, gru_h)
            gru_h = self.gru_drop(gru_h)

            word_logits = self.word_restore(gru_h)
            use_teacher_forcing = not infer and (random.random() < teacher_forcing_ratio)
            if use_teacher_forcing:
                # teacher forcing模式
                word_id = captions[:, i]
            else:
                # 非 teacher forcing模式
                word_id = word_logits.max(1)[1]
            if infer:
                # 如果是推断模式，直接返回单词id
                outputs.append(word_id)
            else:
                # 否则是训练模式，要返回logits
                outputs.append(word_logits)
            # 确定下一个输入单词的表示
            word = self.word_embed(word_id).squeeze(1)
            word = self.word_drop(word)
        # unsqueeze(1)会把一个向量(n)拉成列向量(nx1)
        # outputs中的每一个向量都是整个batch在某个时间步的输出
        # 把它拉成列向量之后再横着拼起来，就能得到整个batch在所有时间步的输出
        outputs = torch.cat([o.unsqueeze(1) for o in outputs], 1).contiguous()
        return outputs

    def sample(self, video_feats):
        '''
        sample就是不给caption且不用teacher forcing的forward
        '''
        return self.forward(video_feats, None, teacher_forcing_ratio=0.0)

    def decode_tokens(self, tokens):
        '''
        根据word id（token）列表和给定的字典来得到caption
        '''
        words = []
        for token in tokens:
            if token == self.vocab('<end>'):
                break
            word = self.vocab.idx2word[token]
            words.append(word)
        caption = ' '.join(words)
        return caption


class BANet(nn.Module):
    def __init__(self, frame_size, projected_size, mid_size, hidden_size,
                 max_frames, max_words, vocab):
        super(BANet, self).__init__()
        self.encoder = Encoder(frame_size, projected_size, mid_size, hidden_size,
                               max_frames)
        self.decoder = Decoder(hidden_size, projected_size, hidden_size,
                               max_words, vocab)

    def forward(self, videos, captions, teacher_forcing_ratio=0.5):
        video_encoded = self.encoder(videos)
        output = self.decoder(video_encoded, captions, teacher_forcing_ratio)
        return output, video_encoded