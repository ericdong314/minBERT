'''
Multitask BERT class, starter training code, evaluation, and test code.

Of note are:
* class MultitaskBERT: Your implementation of multitask BERT.
* function train_multitask: Training procedure for MultitaskBERT. Starter code
    copies training procedure from `classifier.py` (single-task SST).
* function test_multitask: Test procedure for MultitaskBERT. This function generates
    the required files for submission.

Running `python multitask_classifier.py` trains and tests your MultitaskBERT and
writes all required submission files.
'''

import random, numpy as np, argparse
from types import SimpleNamespace

import torch
from sympy.utilities.iterables import iterable
from torch import nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, RandomSampler
from torch.utils.tensorboard import SummaryWriter

from bert import BertModel
from optimizer import AdamW
from tqdm import tqdm

from datasets import (
    SentenceClassificationDataset,
    SentenceClassificationTestDataset,
    SentencePairDataset,
    SentencePairTestDataset,
    load_multitask_data
)

from evaluation import model_eval_sst, model_eval_multitask, model_eval_test_multitask


TQDM_DISABLE=False


# Fix the random seed.
def seed_everything(seed=11711):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


BERT_HIDDEN_SIZE = 768
N_SENTIMENT_CLASSES = 5


class MultitaskBERT(nn.Module):
    '''
    This module should use BERT for 3 tasks:

    - Sentiment classification (predict_sentiment)
    - Paraphrase detection (predict_paraphrase)
    - Semantic Textual Similarity (predict_similarity)
    '''
    def __init__(self, config):
        super(MultitaskBERT, self).__init__()
        self.bert = BertModel.from_pretrained('bert-base-uncased')
        # last-linear-layer mode does not require updating BERT paramters.
        assert config.fine_tune_mode in ["last-linear-layer", "full-model"]
        for param in self.bert.parameters():
            if config.fine_tune_mode == 'last-linear-layer':
                param.requires_grad = False
            elif config.fine_tune_mode == 'full-model':
                param.requires_grad = True
        # You will want to add layers here to perform the downstream tasks.
        ### TODO
        self.num_labels = config.num_labels
        self.dropout = torch.nn.Dropout(config.hidden_dropout_prob)
        self.sst_dense = torch.nn.Linear(config.hidden_size, self.num_labels)
        self.para_dense = torch.nn.Linear(config.hidden_size, 1)
        self.para_dense_siamese = torch.nn.Linear(config.hidden_size * 2, 1)
        self.sts_dense = torch.nn.Linear(config.hidden_size, 1)
        self.sts_dense_siamese = torch.nn.Linear(config.hidden_size* 2, 1)
        self.siamese = config.siamese


    def forward(self, input_ids, attention_mask):
        'Takes a batch of sentences and produces embeddings for them.'
        # The final BERT embedding is the hidden state of [CLS] token (the first token)
        # Here, you can start by just returning the embeddings straight from BERT.
        # When thinking of improvements, you can later try modifying this
        # (e.g., by adding other layers).
        ### TODO
        return self.bert(input_ids, attention_mask)


    def predict_sentiment(self, input_ids, attention_mask):
        '''Given a batch of sentences, outputs logits for classifying sentiment.
        There are 5 sentiment classes:
        (0 - negative, 1- somewhat negative, 2- neutral, 3- somewhat positive, 4- positive)
        Thus, your output should contain 5 logits for each sentence.
        '''
        ### TODO
        pooler_output = self.forward(input_ids, attention_mask)['pooler_output']
        return self.sst_dense(self.dropout(pooler_output))


    def predict_paraphrase(self,
                           input_ids_1, attention_mask_1,
                           input_ids_2, attention_mask_2):
        '''Given a batch of pairs of sentences, outputs a single logit for predicting whether they are paraphrases.
        Note that your output should be unnormalized (a logit); it will be passed to the sigmoid function
        during evaluation.
        '''
        ### TODO
        if not self.siamese:
            pooler_output = self.forward(input_ids_1, attention_mask_1)['pooler_output']
            return self.para_dense(self.dropout(pooler_output)).squeeze(-1)
        else:
            # Get [CLS] embeddings for both sentences
            pooler_output_1 = self.forward(input_ids_1, attention_mask_1)['pooler_output']
            pooler_output_2 = self.forward(input_ids_2, attention_mask_2)['pooler_output']
            # Concatenate the embeddings
            concat = torch.cat([pooler_output_1, pooler_output_2], dim=1)
            return self.para_dense_siamese(self.dropout(concat)).squeeze(-1)


    def predict_similarity(self,
                           input_ids_1, attention_mask_1,
                           input_ids_2, attention_mask_2):
        '''Given a batch of pairs of sentences, outputs a single logit corresponding to how similar they are.
        Note that your output should be unnormalized (a logit).
        '''
        ### TODO
        if not self.siamese:
            pooler_output = self.forward(input_ids_1, attention_mask_1)['pooler_output']
            return self.sts_dense(self.dropout(pooler_output)).squeeze(-1)
        else:
            # Get [CLS] embeddings for both sentences
            pooler_output_1 = self.forward(input_ids_1, attention_mask_1)['pooler_output']
            pooler_output_2 = self.forward(input_ids_2, attention_mask_2)['pooler_output']
            # Concatenate the embeddings
            concat = torch.cat([pooler_output_1, pooler_output_2], dim=1)
            return self.sts_dense_siamese(self.dropout(concat)).squeeze(-1)




def save_model(model, optimizer, args, config, filepath):
    save_info = {
        'model': model.state_dict(),
        'optim': optimizer.state_dict(),
        'args': args,
        'model_config': config,
        'system_rng': random.getstate(),
        'numpy_rng': np.random.get_state(),
        'torch_rng': torch.random.get_rng_state(),
    }

    torch.save(save_info, filepath)
    print(f"save the model to {filepath}")


def train_multitask(args):
    '''Train MultitaskBERT.

    Currently only trains on SST dataset. The way you incorporate training examples
    from other datasets into the training procedure is up to you. To begin, take a
    look at test_multitask below to see how you can use the custom torch `Dataset`s
    in datasets.py to load in examples from the Quora and SemEval datasets.
    '''
    def cycle(iterable):
        while True:
            for it in iterable:
                yield it

    def compute_alpha(epoch, total_epoch, alpha_start=1.0, alpha_end=0.2, linear_decay=True):
        """Annealing of alpha from alpha_start to alpha_end."""
        assert 0 <= epoch < total_epoch
        if linear_decay:
            drop = alpha_start - alpha_end
            return alpha_start - drop * epoch / (total_epoch-1)
        else: # Exponential Decay
            ratio = epoch / (total_epoch-1)
            return alpha_start * ((alpha_end / alpha_start) ** ratio)

    def get_task_probs(alpha, sizes):
        """Return normalized task sampling probabilities using annealed exponent."""
        scaled = sizes ** alpha
        return scaled / scaled.sum()

    device = torch.device('cuda') if args.use_gpu else torch.device('cpu')
    # Create the data and its corresponding datasets and dataloader.
    sst_train_data, num_labels,para_train_data, sts_train_data = load_multitask_data(args.sst_train,args.para_train,args.sts_train, split ='train')
    sst_dev_data, num_labels,para_dev_data, sts_dev_data = load_multitask_data(args.sst_dev,args.para_dev,args.sts_dev, split ='dev')

    sst_dev_data = SentenceClassificationDataset(sst_dev_data, args)
    sst_dev_dataloader = DataLoader(sst_dev_data, shuffle=False, batch_size=args.batch_size,
                                    collate_fn=sst_dev_data.collate_fn)
    para_dev_data = SentencePairDataset(para_dev_data, args)
    para_dev_dataloader = DataLoader(para_dev_data, shuffle=False, batch_size=args.batch_size,
                                     collate_fn=para_dev_data.collate_fn)
    sts_dev_data = SentencePairDataset(sts_dev_data, args, isRegression=True)
    sts_dev_dataloader = DataLoader(sts_dev_data, shuffle=False, batch_size=args.batch_size,
                                    collate_fn=sts_dev_data.collate_fn)

    sst_train_dataset = SentenceClassificationDataset(sst_train_data, args)
    para_train_dataset = SentencePairDataset(para_train_data, args, isRegression=False)
    sts_train_dataset = SentencePairDataset(sts_train_data, args, isRegression=True)

    task_ids = ['sst', 'para', 'sts']
    datasets = [sst_train_dataset, para_train_dataset, sts_train_dataset]
    loaders_orig = [DataLoader(dataset, sampler=RandomSampler(dataset), batch_size=args.batch_size,
                          collate_fn=dataset.collate_fn) for dataset in datasets]
    loaders = [iter(cycle(loader)) for loader in loaders_orig]  # infinitely iterable iterators
    loaders = dict(zip(task_ids, loaders))

    # Init model.
    config = {'hidden_dropout_prob': args.hidden_dropout_prob,
              'num_labels': num_labels,
              'hidden_size': 768,
              'data_dir': '.',
              'fine_tune_mode': args.fine_tune_mode,
              'siamese': args.siamese}

    config = SimpleNamespace(**config)

    model = MultitaskBERT(config)
    model = model.to(device)
    model.train()
    writer = SummaryWriter()

    lr = args.lr
    optimizer = AdamW(model.parameters(), lr=lr)
    best_avg_dev_acc = 0

    for epoch in range(args.epochs):
        model.train()
        alpha = compute_alpha(epoch, args.epochs, 1.0, 0.2, linear_decay=True)
        probs = get_task_probs(alpha, np.array([len(dataset) for dataset in datasets]))
        writer.add_scalars("Sampling Prob", dict(zip(task_ids, probs)), epoch)

        num_steps = 300_000//args.batch_size
        num_steps = 10000
        for step in tqdm(range(num_steps), f'train-{epoch}', disable=TQDM_DISABLE):   # total examples / batch_size
            task_id = np.random.choice(task_ids, p=probs)
            batch = next(loaders[task_id])
            if task_id in ['sts', 'para']:
                (b_ids1, b_mask1,
                 b_ids2, b_mask2,
                 b_labels, b_sent_ids) = (batch['token_ids_1'], batch['attention_mask_1'],
                                          batch['token_ids_2'], batch['attention_mask_2'],
                                          batch['labels'], batch['sent_ids'])

                b_ids1 = b_ids1.to(device)
                b_mask1 = b_mask1.to(device)
                b_ids2 = b_ids2.to(device)
                b_mask2 = b_mask2.to(device)
                b_labels = b_labels.to(device)

                optimizer.zero_grad()
                if task_id == 'sts':
                    logits = model.predict_similarity(b_ids1, b_mask1, b_ids2, b_mask2)
                    loss = F.mse_loss(logits, b_labels.float(), reduction='mean')

                else: # para
                    logits = model.predict_paraphrase(b_ids1, b_mask1, b_ids2, b_mask2)
                    loss = F.binary_cross_entropy_with_logits(logits, b_labels.float(), reduction='mean')

                loss.backward()
                optimizer.step()
            else: # sst
                b_ids, b_mask, b_labels = (batch['token_ids'],
                                           batch['attention_mask'], batch['labels'])

                b_ids = b_ids.to(device)
                b_mask = b_mask.to(device)
                b_labels = b_labels.to(device)

                optimizer.zero_grad()
                logits = model.predict_sentiment(b_ids, b_mask)
                loss = F.cross_entropy(logits, b_labels.view(-1), reduction='sum') / args.batch_size

                loss.backward()
                optimizer.step()

        train_sentiment_accuracy, _, _, \
            train_paraphrase_accuracy, _, _, \
            train_sts_corr, _, _ = model_eval_multitask(loaders_orig[0], loaders_orig[1], loaders_orig[2], model, device)
        dev_sentiment_accuracy, _, _, \
            dev_paraphrase_accuracy, _, _, \
            dev_sts_corr, _, _ = model_eval_multitask(sst_dev_dataloader,para_dev_dataloader,sts_dev_dataloader,model,device)

        avg_dev_acc = (dev_sentiment_accuracy + dev_paraphrase_accuracy + dev_sts_corr) / 3
        avg_train_acc = (train_sentiment_accuracy + train_paraphrase_accuracy + train_sts_corr) / 3
        if avg_dev_acc > best_avg_dev_acc:
            best_avg_dev_acc = avg_dev_acc
            save_model(model, optimizer, args, config, args.filepath)
        train_acc = {'sst':train_sentiment_accuracy, 'para': train_paraphrase_accuracy, 'sts': train_sts_corr, 'avg': avg_train_acc}
        dev_acc = {'sst':dev_sentiment_accuracy, 'para': dev_paraphrase_accuracy, 'sts': dev_sts_corr, 'avg': avg_dev_acc}
        writer.add_scalars('train acc', train_acc, epoch)
        writer.add_scalars('dev acc', dev_acc, epoch)
        print(f"Epoch {epoch}:  dev acc - "
              f"sst::{dev_sentiment_accuracy :.3f}, "
              f"para::{dev_paraphrase_accuracy :.3f}, "
              f"sts::{dev_sts_corr :.3f}" 
              f"avg::{avg_dev_acc :.3f}")

def test_multitask(args):
    '''Test and save predictions on the dev and test sets of all three tasks.'''
    with torch.no_grad():
        device = torch.device('cuda') if args.use_gpu else torch.device('cpu')
        saved = torch.load(args.filepath, weights_only=False)
        config = saved['model_config']

        model = MultitaskBERT(config)
        model.load_state_dict(saved['model'])
        model = model.to(device)
        print(f"Loaded model to test from {args.filepath}")

        sst_test_data, num_labels,para_test_data, sts_test_data = \
            load_multitask_data(args.sst_test,args.para_test, args.sts_test, split='test')

        sst_dev_data, num_labels,para_dev_data, sts_dev_data = \
            load_multitask_data(args.sst_dev,args.para_dev,args.sts_dev,split='dev')

        sst_test_data = SentenceClassificationTestDataset(sst_test_data, args)
        sst_dev_data = SentenceClassificationDataset(sst_dev_data, args)

        sst_test_dataloader = DataLoader(sst_test_data, shuffle=True, batch_size=args.batch_size,
                                         collate_fn=sst_test_data.collate_fn)
        sst_dev_dataloader = DataLoader(sst_dev_data, shuffle=False, batch_size=args.batch_size,
                                        collate_fn=sst_dev_data.collate_fn)

        para_test_data = SentencePairTestDataset(para_test_data, args)
        para_dev_data = SentencePairDataset(para_dev_data, args)

        para_test_dataloader = DataLoader(para_test_data, shuffle=True, batch_size=args.batch_size,
                                          collate_fn=para_test_data.collate_fn)
        para_dev_dataloader = DataLoader(para_dev_data, shuffle=False, batch_size=args.batch_size,
                                         collate_fn=para_dev_data.collate_fn)

        sts_test_data = SentencePairTestDataset(sts_test_data, args)
        sts_dev_data = SentencePairDataset(sts_dev_data, args, isRegression=True)

        sts_test_dataloader = DataLoader(sts_test_data, shuffle=True, batch_size=args.batch_size,
                                         collate_fn=sts_test_data.collate_fn)
        sts_dev_dataloader = DataLoader(sts_dev_data, shuffle=False, batch_size=args.batch_size,
                                        collate_fn=sts_dev_data.collate_fn)

        dev_sentiment_accuracy,dev_sst_y_pred, dev_sst_sent_ids, \
            dev_paraphrase_accuracy, dev_para_y_pred, dev_para_sent_ids, \
            dev_sts_corr, dev_sts_y_pred, dev_sts_sent_ids = model_eval_multitask(sst_dev_dataloader,
                                                                    para_dev_dataloader,
                                                                    sts_dev_dataloader, model, device)

        test_sst_y_pred, \
            test_sst_sent_ids, test_para_y_pred, test_para_sent_ids, test_sts_y_pred, test_sts_sent_ids = \
                model_eval_test_multitask(sst_test_dataloader,
                                          para_test_dataloader,
                                          sts_test_dataloader, model, device)

        with open(args.sst_dev_out, "w+") as f:
            print(f"dev sentiment acc :: {dev_sentiment_accuracy :.3f}")
            f.write(f"id \t Predicted_Sentiment \n")
            for p, s in zip(dev_sst_sent_ids, dev_sst_y_pred):
                f.write(f"{p} , {s} \n")

        with open(args.sst_test_out, "w+") as f:
            f.write(f"id \t Predicted_Sentiment \n")
            for p, s in zip(test_sst_sent_ids, test_sst_y_pred):
                f.write(f"{p} , {s} \n")

        with open(args.para_dev_out, "w+") as f:
            print(f"dev paraphrase acc :: {dev_paraphrase_accuracy :.3f}")
            f.write(f"id \t Predicted_Is_Paraphrase \n")
            for p, s in zip(dev_para_sent_ids, dev_para_y_pred):
                f.write(f"{p} , {s} \n")

        with open(args.para_test_out, "w+") as f:
            f.write(f"id \t Predicted_Is_Paraphrase \n")
            for p, s in zip(test_para_sent_ids, test_para_y_pred):
                f.write(f"{p} , {s} \n")

        with open(args.sts_dev_out, "w+") as f:
            print(f"dev sts corr :: {dev_sts_corr :.3f}")
            f.write(f"id \t Predicted_Similiary \n")
            for p, s in zip(dev_sts_sent_ids, dev_sts_y_pred):
                f.write(f"{p} , {s} \n")

        with open(args.sts_test_out, "w+") as f:
            f.write(f"id \t Predicted_Similiary \n")
            for p, s in zip(test_sts_sent_ids, test_sts_y_pred):
                f.write(f"{p} , {s} \n")


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sst_train", type=str, default="data/ids-sst-train.csv")
    parser.add_argument("--sst_dev", type=str, default="data/ids-sst-dev.csv")
    parser.add_argument("--sst_test", type=str, default="data/ids-sst-test-student.csv")

    parser.add_argument("--para_train", type=str, default="data/quora-train.csv")
    parser.add_argument("--para_dev", type=str, default="data/quora-dev.csv")
    parser.add_argument("--para_test", type=str, default="data/quora-test-student.csv")

    parser.add_argument("--sts_train", type=str, default="data/sts-train.csv")
    parser.add_argument("--sts_dev", type=str, default="data/sts-dev.csv")
    parser.add_argument("--sts_test", type=str, default="data/sts-test-student.csv")

    parser.add_argument("--seed", type=int, default=11711)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--fine-tune-mode", type=str,
                        help='last-linear-layer: the BERT parameters are frozen and the task specific head parameters are updated; full-model: BERT parameters are updated as well',
                        choices=('last-linear-layer', 'full-model'), default="last-linear-layer")
    parser.add_argument("--use_gpu", action='store_true')

    parser.add_argument("--sst_dev_out", type=str, default="predictions/sst-dev-output.csv")
    parser.add_argument("--sst_test_out", type=str, default="predictions/sst-test-output.csv")

    parser.add_argument("--para_dev_out", type=str, default="predictions/para-dev-output.csv")
    parser.add_argument("--para_test_out", type=str, default="predictions/para-test-output.csv")

    parser.add_argument("--sts_dev_out", type=str, default="predictions/sts-dev-output.csv")
    parser.add_argument("--sts_test_out", type=str, default="predictions/sts-test-output.csv")

    parser.add_argument("--batch_size", help='sst: 64, cfimdb: 8 can fit a 12GB GPU', type=int, default=8)
    parser.add_argument("--hidden_dropout_prob", type=float, default=0.3)
    parser.add_argument("--lr", type=float, help="learning rate", default=1e-5)

    # new args
    parser.add_argument('--siamese', action='store_true')
    parser.add_argument('--test_only', action='store_true')
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = get_args()
    siamese = 'siamese'if args.siamese else 'concate'
    args.filepath = f'{siamese}-{args.fine_tune_mode}-{args.epochs}-{args.lr}-multitask.pt' # Save path.
    seed_everything(args.seed)  # Fix the seed for reproducibility.
    if not args.test_only:
        train_multitask(args)
    test_multitask(args)
