# modified from https://github.com/merlresearch/SMART

import os
import random

from comet_ml import Experiment
from comet_ml.integration.pytorch import log_model

import numpy as np
import torch

os.environ["TOKENIZERS_PARALLELISM"] = "1"

import warnings

warnings.filterwarnings("ignore")
import argparse
import copy
import time

import torch.nn.functional as F
from tqdm import tqdm

import vocab_utils
import data_utils as dl
import model_utils as gv
import losses
import models
import puzzle_utils

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

print("You are using device: %s" % device)


experiment = Experiment(
    api_key="",
    project_name="t",
    workspace="",
)

def reset_state(args):
    #    global seed
    gv.seed = np.random.randint(10000) if args.seed == -1 else args.seed

    args.seed = gv.seed  # set at 0
    manualSeed = gv.seed  # set at 0
    np.random.seed(manualSeed)
    torch.manual_seed(manualSeed)
    torch.cuda.manual_seed(manualSeed)
    torch.cuda.manual_seed_all(manualSeed)
    torch.backends.cudnn.deterministic = True
    # added
    torch.manual_seed(manualSeed)
    np.random.seed(manualSeed)
    random.seed(manualSeed)
    print("seed = %d" % (gv.seed))


def train(args, dataloader, im_backbone):
    criterion = losses.Criterion(args)
    
    experiment.log_parameters(args)

    model = models.Puzzle_Net(args, im_backbone=im_backbone)
    model = model.to(device)
    log_model(experiment, model, model_name="TheModel")


    parameters = model.parameters()

    def normalize(err, pids):
        """this function divides the error by the gt number of classes for each puzzle."""
        pids = np.array(pids)
        for t in range(len(err)):
            err[t] = err[t] / gv.NUM_CLASSES_PER_PUZZLE[str(pids[t])]
        return err

    def get_result(out, ltype):
        if ltype == "classifier":
            pred_max = F.softmax(out, dim=1).argmax(dim=1).cpu()
        elif ltype == "regression":
            pred_max = torch.floor(out).long().cpu()[:, 0]
        else:
            raise "unknown loss type"

        return pred_max

    def save_model(args, net, acc, epoch, location):
        state = {
            "net": net.state_dict(),
            "acc": acc,
            "epoch": epoch,
        }
        if not os.path.isdir(location):
            os.mkdir(location)
        loc = os.path.join(
            location,
            "ckpt_%s_%s_%s.pth" % (args.model_name, args.word_embed, args.seed),
        )
        print("saving checkpoint at %s" % (loc))
        torch.save(state, loc)

    def train_loop(epoch, train_loader, optimizer):
        model.train()
        tot_loss = 0.0
        for i, b in tqdm(enumerate(train_loader)):
            (im, q, _, a, av, pids) = b
            im = im.to(device)
            q = q.to(device)

            a = a.to(device)

            av = av.to(device)
           

            out = model(im, q, puzzle_ids=pids)
            loss = criterion(out, av, pids)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            tot_loss += loss.item()

        tot_loss /= float(i)
        return tot_loss


    def val_loop(val_loader, model):
        model.eval()
        acc_mean = 0
        cnt = 0
        err_mean = 0
        opt_mean = 0
        puzzle_acc = {}
        with torch.no_grad():
            for i, (im, q, o, a, av, pids) in enumerate(val_loader):
                
                im = im.to(device)

                q = q.to(device)

                o = np.array(o)
                out = model(im, q, puzzle_ids=pids)

                upids = torch.unique(pids)
                acc = 0
                error = 0
                opts_acc = 0
                for t in upids:
                    idx = pids == t
                    tt = t.item()

                    if t not in gv.SEQ_PUZZLES:
                        pred_max = get_result(out[int(tt)], args.loss_type)
                        pacc = (pred_max == av[idx, 0]).sum()
                        perror = normalize(
                            np.abs(pred_max - av[idx, 0]), pids
                        ).sum()
                        oacc = puzzle_utils.get_option_sel_acc(
                            pred_max, o[idx], a[idx], av[idx], t
                        ).sum()
                    else:
                        pred_ans = []
                        pacc = 1
                        for k in range(gv.MAX_DECODE_STEPS):
                            pred_max = get_result(out[int(tt)][k], args.loss_type)
                            pred_ans.append(pred_max)
                            pacc = pacc * (pred_max == av[idx][:, k])
                        pacc = pacc.sum()
                        perror = 0
                        oacc = puzzle_utils.get_option_sel_acc(
                            np.column_stack(pred_ans), o[idx], a[idx], av[idx], t
                        ).sum()

                    if str(tt) in puzzle_acc.keys():
                        puzzle_acc[str(tt)][0] += pacc
                        puzzle_acc[str(tt)][1] += oacc
                        puzzle_acc[str(tt)][2] += idx.sum()
                    else:
                        puzzle_acc[str(tt)] = [pacc, oacc, idx.sum()]
                    # we use the ansewr value here.
                    opts_acc += oacc
                    acc += pacc
                    error += perror
                

                opt_mean += opts_acc
                acc_mean += acc
                err_mean += error
                cnt += len(av)

        return (
            acc_mean / float(cnt),
            err_mean / float(cnt),
            opt_mean / float(cnt),
            puzzle_acc,
        )

    def test_loop(test_loader, model):
        acc, err, opt, puzzle_acc = val_loop(test_loader, model)
        puzzle_utils.print_puzz_acc(args, puzzle_acc, log=True)
        print(
            "***** Final Test Performance: S_acc = %0.2f O_acc = %0.2f Prediction Variance = %0.2f "
            % (acc * 100, opt * 100, err)
        )

    if args.test:
        models.load_pretrained_models(args, args.model_name, model=model)
        test_loop(dataloader["test"], model)
        return

   
    optimizer = torch.optim.Adam(parameters, lr=args.lr, betas=(0.9, 0.99))
        

    train_loader = dataloader["train"]
    val_loader = dataloader["valid"]
    test_loader = dataloader["test"]

    # training loop
    best_model = None
    best_acc = 0
    no_improvement = 0
    num_thresh_epochs = 1
    # stop training if there is no improvement after this.
    print("starting training...")
    for epoch in range(args.num_epochs):
        tt = time.time()
        model.train()
        loss = train_loop(epoch, train_loader, optimizer)
        tt = time.time() - tt

        if epoch % 1 == 0:
            model.eval()
            acc, err, oacc, puz_acc = val_loop(val_loader, model)
            if acc >= best_acc:
                best_epoch = epoch
                best_acc = acc
                best_model = copy.deepcopy(model)
                save_model(args, best_model, acc, epoch, args.location)
                no_improvement = 0
            else:
                no_improvement += 1
                if no_improvement > num_thresh_epochs:
                    print("no training improvement... stopping the training.")
                    puzzle_utils.print_puzz_acc(args, puz_acc, log=args.log)
                    break
            if epoch % args.log_freq == 0:
                print(
                    "%d) Time taken=%f Epoch=%d Train_loss = %f S_acc = %f O_acc=%f Variance = %f Best S_acc (epoch) = %f (%d)\n"
                    % (
                        gv.seed,
                        tt,
                        epoch,
                        loss,
                        acc * 100,
                        oacc * 100,
                        err,
                        best_acc * 100,
                        best_epoch,
                    )
                )
                puzzle_utils.print_puzz_acc(args, puz_acc, log=args.log)

        if epoch % args.log_freq == 0:
            acc, err, oacc, puz_acc = val_loop(test_loader, model)
            print(
                "puzzles %s: val: s_acc/o_acc/var = %f/%f/%f (%d)"
                % (args.puzzles, acc * 100, oacc * 100, err, best_epoch)
            )

    test_loop(test_loader, best_model)


def get_data_loader(
    args, split, batch_size=100, shuffle=True, num_workers=6, pin_memory=True
):
    if split == "train":
        dataset = dl.Puzzle_TrainData(args, split)
        collate_fn = None
    else:
        dataset = dl.Puzzle_ValData(args, split)
        collate_fn = dl.puzzle_collate_fn
    data_loader = torch.utils.data.DataLoader(
        dataset=dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=num_workers,
        pin_memory=pin_memory,
        collate_fn=collate_fn,
    )
    return data_loader


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="SMART dataset")
    parser.add_argument(
        "--puzzles",
        default="all",
        type=str,
        help="comma separated / all / puzzle groups (counting,math etc.)",
    )
    parser.add_argument("--batch_size", default=64, type=int, help="batch size (16)")
    parser.add_argument("--num_epochs", default=1, type=int, help="epoch")
    parser.add_argument("--lr", default=0.001, type=float, help="learning rate (0.001)")
    parser.add_argument("--test_file", type=str, help="csv file for train")
    parser.add_argument(
        "--data_root",
        type=str,
        default="/home/hice1/droberts308/scratch/smart/SMART101-release-v1/SMART101-Data/",
        help="location of the csv files, and location of the images, relative location is provided in the csv file.",
    )
    parser.add_argument(
        "--train_diff", type=str, default="easy", help="easy/medium/hard"
    )
    parser.add_argument(
        "--test_diff", type=str, default="easy", help="easy/medium/hard"
    )
    parser.add_argument(
        "--split_ratio",
        type=str,
        default="80:5:15",
        help="how to split train and val, when both use the same instance list.",
    )
    parser.add_argument(
        "--save_root",
        type=str,
        default="/home/hice1/droberts308/scratch/saved_stuff",
        help="location to save intermediate files.",
    )
    parser.add_argument(
        "--vocab_path",
        type=str,
        default="none",
        help="location to save intermediate files.",
    )
    parser.add_argument("--num_workers", type=int, default=16, help="number of workers")
    parser.add_argument("--pretrained", type=str, help="should use a pretrained model?")
    parser.add_argument(
        "--optimizer", type=str, default="adam", help="optimizer to use"
    )
    parser.add_argument(
        "--loss_type", type=str, default="classifier", help="classifier/regression"
    )
    parser.add_argument(
        "--model_name", default="resnet50", type=str, help="model to use resnet50/resnet18/..."
    )
    parser.add_argument("--seed", type=int, default=0, help="seed to use")
    parser.add_argument(
        "--data_tot",
        type=int,
        default=2000,
        help="how many instances to use for train+val+test",
    )
    parser.add_argument(
        "--use_clip_text", action="store_true", help="should use clip text embeddings?"
    )
   
    parser.add_argument(
        "--log", action="store_true", help="should print detailed log of accuracy?"
    )
    
    parser.add_argument(
        "--split_type",
        type=str,
        default="standard",
        help="type of data split: standard/exclude/puzzle/fewshot",
    )
    parser.add_argument(
        "--word_embed", type=str, default="bert", help="standard/gpt/bert"
    )
    parser.add_argument(
        "--use_single_image_head",
        action="store_true",
        help="use a single image head for all the puzzles?",
    )
    parser.add_argument(
        "--fsK",
        type=int,
        default=100,
        help="how many samples should we use to train in a fewshot setting?",
    )
    parser.add_argument("--log_freq", type=int, default=50, help="log frequency?")
    parser.add_argument("--test", action="store_true", help="evaluate a model?")
    parser.add_argument(
        "--train_backbone", action="store_true", help="train the image backbone?"
    )
   
    parser.add_argument(
        "--feat_size",
        type=int,
        default=128,
        help="intermediate feature size for image and language features?",
    )

    args = parser.parse_args()


    if args.test:
        assert (
            args.seed > -1
        )  # when evaluating we need to use the seed to take the checkpoint.

    gv.globals_init(args)

    args.puzzle_ids_str, args.puzzle_ids = puzzle_utils.get_puzzle_ids(args)
    args.location = os.path.join(args.save_root, "checkpoints")
    args.log_path = os.path.join(args.save_root, "log")

    reset_state(args)
    gv.NUM_CLASSES_PER_PUZZLE = puzzle_utils.get_puzzle_class_info(
        args
    )  # initialize the global with the number of outputs for each puzzle.

    vocab = vocab_utils.process_text_for_puzzle(args)
    if args.vocab_path == "none":
        args.vocab_path = os.path.join(
            args.save_root, "vocab_puzzle_" + args.puzzle_ids_str + ".pkl"
        )

    im_backbone, preprocess = models.load_pretrained_models(
        args, args.model_name, model=None
    )
    args.preprocess = preprocess

    train_loader = get_data_loader(
        args,
        "train",
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
    )
    val_loader = get_data_loader(
        args,
        "val",
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
    )
    test_loader = get_data_loader(
        args,
        "test",
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
    )

    dataloader = {
        "train": train_loader,
        "valid": val_loader,
        "test": test_loader,
    }

    puzzle_utils.backup_code_and_start_logger(args, args.log_path, args.seed)

    print(args)
    print("num_puzzles=%d" % (len(args.puzzle_ids)))

    train(args, dataloader, im_backbone)
