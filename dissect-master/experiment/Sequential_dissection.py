# New-style dissection experiment code.
import numpy as np
import torch, argparse, os, shutil, inspect, json, random,sys
import torchvision.datasets

sys.path.append("..")
from collections import defaultdict
from netdissect import pbar, nethook, renormalize, zdataset
from netdissect import upsample, tally, imgviz, imgsave, bargraph
import setting
#import .netdissect
torch.backends.cudnn.benchmark = True
from torchvision.utils import save_image
import torchvision.transforms as tt
import torch.nn as nn
import os
import torchcam
from torchcam.methods import GradCAM
from torch.utils.data import DataLoader
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:256"

class Dc_model(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(512, 2)

    def forward(self, x):
        x = self.linear1(x)
        return x

def parseargs():
    parser = argparse.ArgumentParser()
    def aa(*args, **kwargs):
        parser.add_argument(*args, **kwargs)
    aa('--model', default='progan')
    aa('--dataset', default='celebhq')
    aa('--layer', default='layer6')
    aa('--batch_size',type=int, default=1)
    aa('--quantile', type=float, default=0.005)
    aa('--dissect_units_perscent', type=float, default=0.2)
    args = parser.parse_args()
    return args
stats = (0.5, 0.5, 0.5), (0.5, 0.5, 0.5)
def denorm(img_tensor):
    return img_tensor*stats[1][0] + stats[0][0]

def save_images(model,dataset,sample_size,batch_size,path):
        with torch.no_grad():
            img_index=0
            loader =tally.make_loader(dataset, sample_size=sample_size, batch_size=batch_size)
            for batch in loader:
                torch.cuda.empty_cache()
                data_batch = batch[0].cuda()
                fake_images = model(data_batch)
                for i in range(fake_images.shape[0]):
                    save_image(denorm(fake_images[i]), path+'/{}-beforeimages.png'.format(img_index))
                    img_index+=1

def acquire_gradcam_mask(path,batch_size):
    if not os.path.exists(path+'\mask.pth'):
    #loader = tally.make_loader(dataset, sample_size=sample_size, batch_size=batch_size)
        test_transforms = tt.Compose([
            tt.Resize((512, 512)),
            tt.ToTensor(),
            tt.Normalize([0.5, 0.5, 0.5],[0.5, 0.5, 0.5])
            ])
        ds=torchvision.datasets.ImageFolder(path+'generation/',transform=test_transforms)
        dl=DataLoader(ds, batch_size=batch_size)
        resnet = torch.load('model/resnet18_93.pth')
        #resnet.load_state_dict(torch.load('checkpoint.pt'))
        resnet=resnet.cuda()
        cam_extractor = GradCAM(resnet, 'layer4')
        resnet=resnet.eval()
        masks=torch.zeros((len(dl),1,16,16)).cuda()
        index=0
        for fake_images,_ in dl:
            #for i in range(10):
                # sample = torch.unsqueeze(fake_images[i], dim=0)
            #sample = fake_images[0]
            #sample = torch.unsqueeze(sample, dim=0)
            fake_images=fake_images.cuda()
            fake_images.requires_grad_()
            y_pred = resnet.forward(fake_images)

            mask = cam_extractor(y_pred.squeeze(0).argmax().item(), y_pred, retain_graph=True)
            #mask_resize = tt.Resize(64)(torch.unsqueeze(mask[0], dim=0))
            masks[index, :] = mask[0]
            index+=1
        torch.save(masks,path+'\mask.pth')
    else:
        masks=torch.load(path+'\mask.pth')
    return masks

def acquire_activation(args,path,model,dataset,layername,level_at_99,sample_size,batch_size,upfn):
    if not os.path.exists(path+'/acts%s.pth'%layername):
        loader = tally.make_loader(dataset, sample_size=sample_size, batch_size=batch_size)
        acts=torch.zeros(sample_size,512,16,16).cuda()
        batch_index=0
        for batch in loader:
            data_batch = batch[0].cuda()
            fake_images = model(data_batch)
            act= model.retained_layer(layername)
            hact = make_upfn(args, dataset, model, layername)(act)
            hact=upfn(act)
            iact = (hact > level_at_99).float() # indicator
            #iact=torch.where((hact>level_at_99)[0],hact[0],acts[batch_index])
            #iact = torch.tensor(hact > level_at_99, dtype=int)
            acts[batch_index]=iact
            batch_index+=1

        torch.save(acts,path+'/acts%s.pth'%layername)
    else:
        acts=torch.load(path+'/acts%s.pth'%layername)
    return acts

def acquire_dsscore(path,acts,masks,sample_size,layername):
    if not os.path.exists(path + '/ds_score_%s.pth'%layername):
        ds_score=torch.zeros(acts.shape[1])
        acts=acts.cuda()
        for unit in range(acts.shape[1]):#对每个unit操作
            iou_each_unit=0
            for sample_index in range(sample_size):
                masks_mean=(masks[sample_index][0].sum()/torch.count_nonzero(masks[sample_index][0]).item()).item()
                if masks_mean is None:
                    masks_mean=0
                isect=acts[sample_index,unit]*masks[sample_index][0]
                union=acts[sample_index,unit]*masks_mean
                iou=isect/union
                iou = torch.where(torch.isnan(iou), torch.full_like(iou, 0), iou).sum()
                #iou_each_unit+=iou
                iou_each_unit=isect.sum()
            ds_score[unit]=iou_each_unit/sample_size
        torch.save(ds_score,path +'/ds_score_%s.pth'%layername)
    else:
        ds_score=torch.load(path+'/ds_score_%s.pth'%layername)
    return ds_score

def dissection(model,dissect_unit,layernames,dataset,path,sample_size,batch_size,ds_score):
    layertime=1
    lambda_value=0.9
    def zero_some_units(layer,x ,*args):

        x[:, dissect_unit[int(layer[-1])-1]] = 0
        return x
    def dsscore_partial(layer,x ,*args):
        for unit in dissect_unit[int(layer[-1])-1]:
            d_min=ds_score[int(layer[-1])-1].min()
            d_max=ds_score[int(layer[-1])-1].max()
            dst=d_max-d_min
            norm_data=(ds_score[int(layer[-1])-1]-d_min).true_divide(dst)
            #print(unit)
            x[:,unit] =lambda_value*(1-norm_data[unit])*x[:,unit]
        return x
        #pass
    loader = tally.make_loader(dataset, sample_size=sample_size, batch_size=batch_size)
    index=0
    if not os.path.exists(path+'/images/'):
        os.makedirs(path+'/images/')
    for sample in loader:
        with torch.no_grad():

            model.remove_edits()
            sample = sample[0].cuda()
            before_img = model(sample)
            save_image(denorm(before_img[0]), path + '/images/{}-before_images.png'.format(index))
            #actbefore=
            for layer in layernames:
                model.edit_layer(layer, rule=dsscore_partial)
            after_img=model(sample)
            save_image(denorm(after_img[0]), path+'/images/{}-after_images.png'.format(index))
            index+=1


    model.remove_edits()
    # def zero_some_units(x, *args):
    #     x[:, max_unit] = 0
    #     return x
    # model.edit_layer('layer4', rule=zero_some_units)
    #
    # def measure_segclasses_with_zeroed_units(zeroed_units, sample_size=100):
    #     model.remove_edits()
    #     def zero_some_units(x, *args):
    #         x[:, zeroed_units] = 0
    #         return x
    #     model.edit_layer(layername, rule=zero_some_units)
    #     num_seglabels = len(segmodel.get_label_and_category_names()[0])
    #     def compute_mean_seg_in_images(batch_z, *args):
    #         img = model(batch_z.cuda())
    #         seg = segmodel.segment_batch(img, downsample=4)
    #         seg_area = seg.shape[2] * seg.shape[3]
    #         seg_counts = torch.bincount((seg + (num_seglabels *
    #             torch.arange(seg.shape[0],
    #                 dtype=seg.dtype, device=seg.device
    #                 )[:,None,None,None])).view(-1),
    #             minlength=num_seglabels * seg.shape[0]).view(seg.shape[0], -1)
    #         seg_fracs = seg_counts.float() / seg_area
    #         return seg_fracs
    #     result = tally.tally_mean(compute_mean_seg_in_images, dataset,
    #                             batch_size=30, sample_size=sample_size, pin_memory=True)
    #     model.remove_edits()
    #     return result

    # Intervention experiment here:
    # segs_baseline = measure_segclasses_with_zeroed_units([])
    # segs_without_treeunits = measure_segclasses_with_zeroed_units(tree_units)
    # num_units = len(unit_label_99)
    # baseline_segmean = test_generator_segclass_stats(
    #             model, dataset, segmodel,
    #             layername=layername,
    #             cachefile=resfile('segstats/baseline.npz')).mean()
    #
    # pbar.descnext('unit ablation')
    # unit_ablation_segmean = torch.zeros(num_units, len(baseline_segmean))
    # for unit in pbar(random.sample(range(num_units), num_units)):
    #     stats = test_generator_segclass_stats(model, dataset, segmodel,
    #         layername=layername, zeroed_units=[unit],
    #         cachefile=resfile('segstats/ablated_unit_%d.npz' % unit))
    #     unit_ablation_segmean[unit] = stats.mean()
    #
    # ablate_segclass_name = 'tree'
    # ablate_segclass = seglabels.index(ablate_segclass_name)
    # best_iou_units = iou_99[ablate_segclass,:].sort(0)[1].flip(0)
    # byiou_unit_ablation_seg = torch.zeros(30)
    # for unitcount in pbar(random.sample(range(0,30), 30)):
    #     zero_units = best_iou_units[:unitcount].tolist()
    #     stats = test_generator_segclass_delta_stats(
    #         model, dataset, segmodel,
    #         layername=layername, zeroed_units=zero_units,
    #         cachefile=resfile('deltasegstats/ablated_best_%d_iou_%s.npz' %
    #                     (unitcount, ablate_segclass_name)))
    #     byiou_unit_ablation_seg[unitcount] = stats.mean()[ablate_segclass]
    #
    # # Generator context experiment.
    # num_segclass = len(seglabels)
    # door_segclass = seglabels.index('door')
    # door_units = iou_99[door_segclass].sort(0)[1].flip(0)[:20]
    # door_high_values = rq.quantiles(0.995)[door_units].cuda()
    #
    # def compute_seg_impact(zbatch, *args):
    #     zbatch = zbatch.cuda()
    #     model.remove_edits()
    #     orig_img = model(zbatch)
    #     orig_seg = segmodel.segment_batch(orig_img, downsample=4)
    #     orig_segcount = tally.batch_bincount(orig_seg, num_segclass)
    #     rep = model.retained_layer(layername).clone()
    #     ysize = orig_seg.shape[2] // rep.shape[2]
    #     xsize = orig_seg.shape[3] // rep.shape[3]
    #     def gen_conditions():
    #         for y in range(rep.shape[2]):
    #             for x in range(rep.shape[3]):
    #                 # Take as the context location the segmentation
    #                 # labels at the center of the square.
    #                 selsegs = orig_seg[:,:,y*ysize+ysize//2,
    #                         x*xsize+xsize//2]
    #                 changed_rep = rep.clone()
    #                 changed_rep[:,door_units,y,x] = (
    #                         door_high_values[None,:])
    #                 model.edit_layer(layername,
    #                         ablation=1.0, replacement=changed_rep)
    #                 changed_img = model(zbatch)
    #                 changed_seg = segmodel.segment_batch(
    #                         changed_img, downsample=4)
    #                 changed_segcount = tally.batch_bincount(
    #                         changed_seg, num_segclass)
    #                 delta_segcount = (changed_segcount
    #                         - orig_segcount).float()
    #                 for sel, delta in zip(selsegs, delta_segcount):
    #                     for cond in torch.bincount(sel).nonzero()[:,0]:
    #                         if cond == 0:
    #                             continue
    #                         yield (cond.item(), delta)
    #     return gen_conditions()
    #
    # cond_changes = tally.tally_conditional_mean(
    #         compute_seg_impact, dataset, sample_size=10000, batch_size=20,
    #         cachefile=resfile('big_door_cond_changes.npz'))

def main():
    args = parseargs()
    resdir = 'results/%s-%s-%s-quantile%s-units%s' % (
            args.model, args.dataset, args.layer,
            int(args.quantile * 1000),float(args.dissect_units_perscent))
    def resfile(f):
        return os.path.join(resdir, f)
##############               准备模型、数据集、参数                   ########################
    model = load_model(args)
    layernames = instrumented_layername(args).split(',')
    dataset = load_dataset(args, model=model.model)
    sample_size = len(dataset)
    batch_size=args.batch_size
    is_generator = (args.model == 'progan')
    percent_level = 1.0 - args.quantile
    dissect_units_perscent=args.dissect_units_perscent

###############           保存原始模型的生成图片                   ############################
    # with torch.no_grad():
    #     if not os.path.exists (resfile('generation/fake')):
    #         os.mkdir(resfile('generation/fake'))
    #         save_images(model,dataset,sample_size,batch_size,resfile('generation/fake'))

    dissect_unit=[]
    ds_score=[]
    for layername in layernames:
        #args.layer=layername
        model.retain_layer(layername)

        upfn = make_upfn(args, dataset, model, layername)

        ###############            计算对应层的激活值、并上采样              ###########################
        pbar.descnext('rq')
        def compute_samples(img_index,batch):
            data_batch = batch.cuda()
            if not os.path.exists(resfile('generation/fake')):
                os.makedirs(resfile('generation/fake'))
            with torch.no_grad():
                fake_images = model(data_batch)
                for i in range(fake_images.shape[0]):
                    save_image(denorm(fake_images[i]), resfile('generation/fake') + '/{}-before_images.png'.format(img_index))
                    img_index += 1
            acts = model.retained_layer(layername)
            hacts =upfn(acts)
            #hacts=acts
            return hacts.permute(0, 2, 3, 1).contiguous().view(-1, acts.shape[1])
        rq = tally.tally_quantile(compute=compute_samples,dataset=dataset,fake_path=resfile('generation/'),
                                  sample_size=sample_size,
                                  r=8192,
                                  num_workers=2,#orginal：100
                                  pin_memory=True,
                                  cachefile=resfile('rq_%s.npz'%layername))

#########################              取quantile值               ##############################
        level_at_99 = rq.quantiles(percent_level).cuda()[None,:,None,None]
        renorm = renormalize.renormalizer(dataset, target='zc')

################    获取各图像的mask     #################################
        masks = acquire_gradcam_mask(resfile(''), batch_size)

################   获取threshold后的activation map，     #################################
        acts=acquire_activation(args,resfile(''),model,dataset, layername,level_at_99, sample_size,batch_size,upfn)


##################    计算DS    ################################
        ds_score.append(acquire_dsscore(resfile(''),acts,masks,sample_size,layername))


#################################   计算最高激活map    ######################################
        sort_score,indices=torch.sort(ds_score[int(layername[-1])-1])
        dissect_unit.append(indices[int(-len(indices)*dissect_units_perscent):])

######################    消融  保存生成图片  ###############################
    dissection(model, dissect_unit, layernames, dataset, resfile(''), sample_size, batch_size,ds_score)

#def compute_conditional_indicator(batch, *args):
    #     data_batch = batch.cuda()
    #     out_batch = model(data_batch)
    #     image_batch = out_batch if is_generator else renorm(data_batch)
    #     seg = segmodel.segment_batch(image_batch, downsample=4)
    #     acts = model.retained_layer(layername)
    #     hacts = upfn(acts)
    #     iacts = (hacts > level_at_99).float() # indicator
    #     return tally.conditional_samples(iacts, seg)
    #
    # pbar.descnext('condi99')
    # condi99 = tally.tally_conditional_mean(compute_conditional_indicator,
    #         dataset, sample_size=sample_size,
    #         num_workers=3, pin_memory=True,
    #         cachefile=resfile('condi99.npz'))

    # Now summarize the iou stats and graph the units
    #iou_99 = tally.iou_from_conditional_indicator_mean(condi99)
    #unit_label_99 = [
    #        (concept.item(), seglabels[concept],
    #            segcatlabels[concept], bestiou.item())
     #       for (bestiou, concept) in zip(*iou_99.max(0))]
def test_generator_segclass_stats(model, dataset, segmodel,
        layername=None, zeroed_units=None, sample_size=None, cachefile=None):
    model.remove_edits()
    if zeroed_units is not None:
        def zero_some_units(x, *args):
            x[:, zeroed_units] = 0
            return x
        model.edit_layer(layername, rule=zero_some_units)
    num_seglabels = len(segmodel.get_label_and_category_names()[0])
    def compute_mean_seg_in_images(batch_z, *args):
        img = model(batch_z.cuda())
        seg = segmodel.segment_batch(img, downsample=4)
        seg_area = seg.shape[2] * seg.shape[3]
        seg_counts = torch.bincount((seg + (num_seglabels *
            torch.arange(seg.shape[0], dtype=seg.dtype, device=seg.device
                )[:,None,None,None])).view(-1),
            minlength=num_seglabels * seg.shape[0]).view(seg.shape[0], -1)
        seg_fracs = seg_counts.float() / seg_area
        return seg_fracs
    result = tally.tally_mean(compute_mean_seg_in_images, dataset,
                            batch_size=25, sample_size=sample_size,
                            pin_memory=True, cachefile=cachefile)
    model.remove_edits()
    return result

def make_upfn(args, dataset, model, layername):
    '''Creates an upsampling function.'''
    convs, data_shape = None, None
    if args.model == 'alexnet':
        convs = [layer for name, layer in model.model.named_children()
                if name.startswith('conv') or name.startswith('pool')]
    elif args.model == 'progan':
        # Probe the data shape
        out = model(dataset[0][0][None,...].cuda())

        data_shape = model.retained_layer(layername).shape[2:]
        upfn = upsample.upsampler(
                (16, 16),
                data_shape=data_shape,
                image_size=out.shape[2:])
        return upfn
    else:
        # Probe the data shape
        _ = model(dataset[0][0][None,...].cuda())
        data_shape = model.retained_layer(layername).shape[2:]
        pbar.print('upsampling from data_shape', tuple(data_shape))
    upfn = upsample.upsampler(
            (56, 56),
            data_shape=data_shape,
            source=dataset,
            convolutions=convs)
    return upfn

def instrumented_layername(args):
    '''Chooses the layer name to dissect.'''
    if args.layer is not None:
        if args.model == 'vgg16':
            return 'features.' + args.layer
        return args.layer
    # Default layers to probe
    if args.model == 'alexnet':
        return 'conv5'
    elif args.model == 'vgg16':
        return 'features.conv5_3'
    elif args.model == 'resnet152':
        return '7'
    elif args.model == 'progan':
        return 'layer4'

def load_model(args):
    '''Loads one of the benchmark classifiers or generators.'''
    if args.model in ['alexnet', 'vgg16', 'resnet152']:
        model = setting.load_classifier(args.model)
    elif args.model == 'progan':
        model = setting.load_proggan(args.dataset)
    model = nethook.InstrumentedModel(model).cuda().eval()
    return model

def load_dataset(args, model=None):
    '''Loads an input dataset for testing.'''
    from torchvision import transforms
    if args.model == 'progan':
        dataset = zdataset.z_dataset_for_model(model, size=100, seed=1)#size=10000
        return dataset
    elif args.dataset in ['places']:
        crop_size = 227 if args.model == 'alexnet' else 224
        return setting.load_dataset(args.dataset, split='val', full=True,
                crop_size=crop_size, download=True)
    assert False

def graph_conceptcatlist(conceptcatlist, **kwargs):
    count = defaultdict(int)
    catcount = defaultdict(int)
    for c in conceptcatlist:
        count[c] += 1
    for c in count.keys():
        catcount[c[1]] += 1
    cats = ['object', 'part', 'material', 'texture', 'color']
    catorder = dict((c, i) for i, c in enumerate(cats))
    sorted_labels = sorted(count.keys(),
        key=lambda x: (catorder[x[1]], -count[x]))
    sorted_labels
    return bargraph.make_svg_bargraph(
        [label for label, cat in sorted_labels],
        [count[k] for k in sorted_labels],
        [(c, catcount[c]) for c in cats], **kwargs)

def save_conceptcat_graph(filename, conceptcatlist):
    svg = graph_conceptcatlist(conceptcatlist, barheight=80, file_header=True)
    with open(filename, 'w') as f:
        f.write(svg)

def test_generator_segclass_stats(model, dataset, segmodel,
        layername=None, zeroed_units=None, sample_size=None, cachefile=None):
    model.remove_edits()
    if zeroed_units is not None:
        def zero_some_units(x, *args):
            x[:, zeroed_units] = 0
            return x
        model.edit_layer(layername, rule=zero_some_units)
    num_seglabels = len(segmodel.get_label_and_category_names()[0])
    def compute_mean_seg_in_images(batch_z, *args):
        img = model(batch_z.cuda())
        seg = segmodel.segment_batch(img, downsample=4)
        seg_area = seg.shape[2] * seg.shape[3]
        seg_counts = torch.bincount((seg + (num_seglabels *
            torch.arange(seg.shape[0], dtype=seg.dtype, device=seg.device
                )[:,None,None,None])).view(-1),
            minlength=num_seglabels * seg.shape[0]).view(seg.shape[0], -1)
        seg_fracs = seg_counts.float() / seg_area
        return seg_fracs
    result = tally.tally_mean(compute_mean_seg_in_images, dataset,
                            batch_size=25, sample_size=sample_size,
                            pin_memory=True, cachefile=cachefile)
    model.remove_edits()
    return result


def test_generator_segclass_delta_stats(model, dataset, segmodel,
        layername=None, zeroed_units=None, sample_size=None, cachefile=None):
    model.remove_edits()
    def zero_some_units(x, *args):
        x[:, zeroed_units] = 0
        return x
    num_seglabels = len(segmodel.get_label_and_category_names()[0])
    def compute_mean_delta_seg_in_images(batch_z, *args):
        # First baseline
        model.remove_edits()
        img = model(batch_z.cuda())
        seg = segmodel.segment_batch(img, downsample=4)
        seg_area = seg.shape[2] * seg.shape[3]
        seg_counts = torch.bincount((seg + (num_seglabels *
            torch.arange(seg.shape[0], dtype=seg.dtype, device=seg.device
                )[:,None,None,None])).view(-1),
            minlength=num_seglabels * seg.shape[0]).view(seg.shape[0], -1)
        seg_fracs = seg_counts.float() / seg_area
        # Then with changes
        model.edit_layer(layername, rule=zero_some_units)
        d_img = model(batch_z.cuda())
        d_seg = segmodel.segment_batch(d_img, downsample=4)
        d_seg_counts = torch.bincount((d_seg + (num_seglabels *
            torch.arange(seg.shape[0], dtype=seg.dtype, device=seg.device
                )[:,None,None,None])).view(-1),
            minlength=num_seglabels * seg.shape[0]).view(seg.shape[0], -1)
        d_seg_fracs = d_seg_counts.float() / seg_area
        return d_seg_fracs - seg_fracs
    result = tally.tally_mean(compute_mean_delta_seg_in_images, dataset,
                            batch_size=25, sample_size=sample_size,
                            pin_memory=True, cachefile=cachefile)
    model.remove_edits()
    return result
class FloatEncoder(json.JSONEncoder):
    def __init__(self, nan_str='"NaN"', **kwargs):
        super(FloatEncoder, self).__init__(**kwargs)
        self.nan_str = nan_str

    def iterencode(self, o, _one_shot=False):
        if self.check_circular:
            markers = {}
        else:
            markers = None
        if self.ensure_ascii:
            _encoder = json.encoder.encode_basestring_ascii
        else:
            _encoder = json.encoder.encode_basestring
        def floatstr(o, allow_nan=self.allow_nan,
                _inf=json.encoder.INFINITY, _neginf=-json.encoder.INFINITY,
                nan_str=self.nan_str):
            if o != o:
                text = nan_str
            elif o == _inf:
                text = '"Infinity"'
            elif o == _neginf:
                text = '"-Infinity"'
            else:
                return repr(o)
            if not allow_nan:
                raise ValueError(
                    "Out of range float values are not JSON compliant: " +
                    repr(o))
            return text

        _iterencode = json.encoder._make_iterencode(
                markers, self.default, _encoder, self.indent, floatstr,
                self.key_separator, self.item_separator, self.sort_keys,
                self.skipkeys, _one_shot)
        return _iterencode(o, 0)

def dump_json_file(target, data):
    with open(target, 'w') as f:
        json.dump(data, f, indent=1, cls=FloatEncoder)

def copy_static_file(source, target):
    sourcefile = os.path.join(
            os.path.dirname(inspect.getfile(netdissect)), source)
    shutil.copy(sourcefile, target)

if __name__ == '__main__':
    main()

