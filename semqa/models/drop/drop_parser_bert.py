import logging
from typing import List, Dict, Any, Tuple, Optional, Set, Union
import numpy as np
import random
from overrides import overrides

import torch
import torch.nn.functional as F

from allennlp.data.fields.production_rule_field import ProductionRule
from allennlp.data.vocabulary import Vocabulary
from allennlp.models.model import Model
from allennlp.modules import Highway, Attention, TextFieldEmbedder, Seq2SeqEncoder
from allennlp.nn import Activation
from allennlp.modules.matrix_attention import MatrixAttention, DotProductMatrixAttention, LinearMatrixAttention
from allennlp.state_machines.states import GrammarBasedState
import allennlp.nn.util as allenutil
from allennlp.nn import InitializerApplicator, RegularizerApplicator
from allennlp.models.reading_comprehension.util import get_best_span
from allennlp.state_machines.transition_functions import BasicTransitionFunction
from allennlp.state_machines.trainers.maximum_marginal_likelihood import MaximumMarginalLikelihood
from allennlp.state_machines import BeamSearch
from allennlp.state_machines import ConstrainedBeamSearch
from semqa.state_machines.constrained_beam_search import ConstrainedBeamSearch as MyConstrainedBeamSearch
from allennlp.training.metrics import Average, DropEmAndF1
from pytorch_pretrained_bert import BertModel

from semqa.models.utils import semparse_utils

from semqa.models.drop.drop_parser_base import DROPParserBase
from semqa.domain_languages.drop import (DropLanguage, Date,
                                         QuestionSpanAnswer, PassageSpanAnswer, YearDifference, PassageNumber,
                                         CountNumber, PassageNumberDifference)
from semqa.domain_languages.drop import ExecutorParameters

import datasets.drop.constants as dropconstants

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

# In this three tuple, add "QENT:qent QSTR:qstr" in the twp gaps, and join with space to get the logical form
GOLD_BOOL_LF = ("(bool_and (bool_qent_qstr ", ") (bool_qent_qstr", "))")


@Model.register("drop_parser_bert")
class DROPParserBERT(DROPParserBase):
    def __init__(self,
                 vocab: Vocabulary,
                 pretrained_bert_model: str,
                 max_ques_len: int,
                 action_embedding_dim: int,
                 transitionfunc_attention: Attention,
                 passage_attention_to_span: Seq2SeqEncoder,
                 question_attention_to_span: Seq2SeqEncoder,
                 passage_attention_to_count: Seq2SeqEncoder,
                 beam_size: int,
                 max_decoding_steps: int,
                 countfixed: bool = False,
                 auxwinloss: bool = False,
                 denotationloss: bool = True,
                 excloss: bool = False,
                 qattloss: bool = False,
                 mmlloss: bool = False,
                 dropout: float = 0.0,
                 debug: bool = False,
                 initializers: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:

        super(DROPParserBERT, self).__init__(vocab=vocab,
                                             action_embedding_dim=action_embedding_dim,
                                             dropout=dropout,
                                             debug=debug,
                                             regularizer=regularizer)

        self.BERT = BertModel.from_pretrained(pretrained_bert_model)
        bert_dim = self.BERT.pooler.dense.out_features
        self.bert_dim = bert_dim

        self.max_ques_len = max_ques_len

        question_encoding_dim = bert_dim

        self._decoder_step = BasicTransitionFunction(encoder_output_dim=question_encoding_dim,
                                                     action_embedding_dim=action_embedding_dim,
                                                     input_attention=transitionfunc_attention,
                                                     activation=Activation.by_name('tanh')(),
                                                     predict_start_type_separately=False,
                                                     num_start_types=1,
                                                     add_action_bias=False,
                                                     dropout=dropout)
        self._mml = MaximumMarginalLikelihood()

        # self.modeltype = modeltype

        self._beam_size = beam_size
        self._decoder_beam_search = BeamSearch(beam_size=self._beam_size)
        self._max_decoding_steps = max_decoding_steps
        self._action_padding_index = -1

        # This metrircs measure accuracy of
        # (1) Top-predicted program, (2) ExpectedDenotation from the beam (3) Best accuracy from topK(5) programs
        self.top1_acc_metric = Average()
        self.expden_acc_metric = Average()
        self.topk_acc_metric = Average()
        self.aux_goldparse_loss = Average()
        self.qent_loss = Average()
        self.qattn_cov_loss_metric = Average()

        # encoding_in_dim = phrase_layer.get_input_dim()
        # encoding_out_dim = phrase_layer.get_output_dim()
        # modeling_in_dim = modeling_layer.get_input_dim()
        # modeling_out_dim = modeling_layer.get_output_dim()
        #
        # self._embedding_proj_layer = torch.nn.Linear(text_embed_dim, encoding_in_dim)
        # self._highway_layer = Highway(encoding_in_dim, num_highway_layers)
        #
        # self._encoding_proj_layer = torch.nn.Linear(encoding_in_dim, encoding_in_dim)
        # self._phrase_layer = phrase_layer

        # Used for modeling
        # self._matrix_attention = matrix_attention_layer

        # self._modeling_proj_layer = torch.nn.Linear(encoding_out_dim * 4, modeling_in_dim)
        # self._modeling_layer = modeling_layer

        # Use a separate encoder for passage - date - num similarity

        self.qp_matrix_attention = LinearMatrixAttention(tensor_1_dim=bert_dim,
                                                         tensor_2_dim=bert_dim,
                                                         combination="x,y,x*y")

        # self.passage_token_to_date = passage_token_to_date
        self.dotprod_matrix_attn = DotProductMatrixAttention()

        if dropout > 0:
            self._dropout = torch.nn.Dropout(p=dropout)
        else:
            self._dropout = lambda x: x

        self.num_counts = 10
        self.passage_attention_to_count = passage_attention_to_count
        self.passage_count_predictor = torch.nn.Linear(self.passage_attention_to_count.get_output_dim(),
                                                       self.num_counts, bias=False)
        self.passage_count_hidden2logits = torch.nn.Linear(self.passage_attention_to_count.get_output_dim(),
                                                           1, bias=True)

        # self.passage_count_predictor.bias.data.zero_()
        # self.passage_count_predictor.bias.requires_grad = False

        self._executor_parameters = ExecutorParameters(question_encoding_dim=bert_dim,
                                                       passage_encoding_dim=bert_dim,
                                                       passage_attention_to_span=passage_attention_to_span,
                                                       question_attention_to_span=question_attention_to_span,
                                                       passage_attention_to_count=self.passage_attention_to_count,
                                                       passage_count_predictor=self.passage_count_predictor,
                                                       passage_count_hidden2logits=self.passage_count_hidden2logits,
                                                       dropout=dropout)

        self.modelloss_metric = Average()
        self.excloss_metric = Average()
        self.qattloss_metric = Average()
        self.mmlloss_metric = Average()
        self.auxwinloss_metric = Average()
        self._drop_metrics = DropEmAndF1()

        self.auxwinloss = auxwinloss

        # Main loss for QA
        self.denotation_loss = denotationloss
        # Auxiliary losses, such as - Prog-MML, QAttn, DateGrounding etc.
        self.excloss = excloss
        self.qattloss = qattloss
        self.mmlloss = mmlloss

        initializers(self)

        for name, parameter in self.named_parameters():
            if name == "_text_field_embedder.token_embedder_tokens.weight":
                parameter.requires_grad = False

        # # # Fix parameters for Counting
        count_parameter_names = ['passage_attention_to_count', 'passage_count_hidden2logits',
                                 'passage_count_predictor']
        if countfixed:
            for name, parameter in self.named_parameters():
                if any(span in name for span in count_parameter_names):
                    parameter.requires_grad = False


        # Fixing Pre-trained parameters
        # pretrained_components = ['text_field_embedder', 'highway_layer', 'embedding_proj_layer',
        #                          'encoding_proj_layer', 'phrase_layer', 'matrix_attention']
        # for name, parameter in self.named_parameters():
        #     if any(component in name for component in pretrained_components):
        #         parameter.requires_grad = False


    @overrides
    def forward(self,
                question_passage: Dict[str, torch.LongTensor],
                question: Dict[str, torch.LongTensor],
                passage: Dict[str, torch.LongTensor],
                passageidx2numberidx: torch.LongTensor,
                passage_number_values: List[List[float]],
                passage_number_sortedtokenidxs: List[List[int]],
                passageidx2dateidx: torch.LongTensor,
                passage_date_values: List[List[Date]],
                actions: List[List[ProductionRule]],
                year_differences: List[List[int]],
                year_differences_mat: List[np.array],
                count_values: List[List[int]],
                passagenumber_difference_values: List[List[float]],
                passagenumber_differences_mat: List[np.array],
                answer_program_start_types: List[Union[List[str], None]] = None,
                answer_as_passage_spans: torch.LongTensor = None,
                answer_as_question_spans: torch.LongTensor = None,
                answer_as_passage_number: List[List[int]] = None,
                answer_as_year_difference: List[List[int]] = None,
                answer_as_count: List[List[int]] = None,
                answer_as_passagenum_difference: List[List[int]] = None,
                datecomp_ques_event_date_groundings: List[Tuple[List[int], List[int]]] = None,
                numcomp_qspan_num_groundings: List[Tuple[List[int], List[int]]] = None,
                strongly_supervised: List[bool] = None,
                program_supervised: List[bool] = None,
                qattn_supervised: List[bool] = None,
                pattn_supervised: List[bool] = None,
                execution_supervised: List[bool] = None,
                qtypes: List[str] = None,
                gold_action_seqs: List[Tuple[List[List[int]], List[List[int]]]]=None,
                qattn_supervision: torch.FloatTensor = None,
                passage_attn_supervision: List[List[float]] = None,
                synthetic_numground_metadata: List[Tuple[int, int]] = None,
                epoch_num: List[int] = None,
                metadata: List[Dict[str, Any]] = None,
                aux_passage_attention=None,
                aux_answer_as_count=None,
                aux_count_mask=None) -> Dict[str, torch.Tensor]:


        question_passage_tokens = question_passage["tokens"]
        pad_mask = question_passage["mask"]
        segment_ids = question_passage["tokens-type-ids"]

        # Padding "[PAD]" tokens in the question
        pad_mask = (question_passage_tokens > 0).long() * pad_mask

        # Shape: (batch_size, seqlen, bert_dim); (batch_size, bert_dim)
        bert_out, bert_pooled_out = self.BERT(question_passage_tokens, segment_ids, pad_mask,
                                              output_all_encoded_layers=False)
        # Skip [CLS]; then the next max_ques_len tokens are question tokens
        encoded_question = bert_out[:, 1:self.max_ques_len + 1, :]
        question_mask = (pad_mask[:, 1:self.max_ques_len + 1]).float()
        # [CLS] Q_tokens [SEP]
        encoded_passage = bert_out[:, 1+self.max_ques_len+1:, :]
        passage_mask = (pad_mask[:, 1+self.max_ques_len+1:]).float()

        batch_size = len(actions)
        device_id = self._get_prediction_device()

        if epoch_num is not None:
            # epoch_num in allennlp starts from 0
            epoch = epoch_num[0] + 1
        else:
            epoch = None

        # rawemb_question = self._dropout(self._text_field_embedder(question))
        # rawemb_passage = self._dropout(self._text_field_embedder(passage))
        #
        # embedded_question = self._highway_layer(self._embedding_proj_layer(rawemb_question))
        # embedded_passage = self._highway_layer(self._embedding_proj_layer(rawemb_passage))
        #
        # passage_length = passage_mask.size()[1]
        #
        # # projected_embedded_question = self._encoding_proj_layer(embedded_question)
        # # projected_embedded_passage = self._encoding_proj_layer(embedded_passage)
        #
        # # stripped version
        # projected_embedded_question = embedded_question
        # projected_embedded_passage = embedded_passage
        #
        # # Shape: (batch_size, question_length, encoding_dim)
        # encoded_question = self._dropout(self._phrase_layer(projected_embedded_question, question_mask))
        # # Shape: (batch_size, passage_length, encoding_dim)
        # encoded_passage = self._dropout(self._phrase_layer(projected_embedded_passage, passage_mask))
        #
        # if self._debug:
        #     rawemb_passage_norm = self.compute_avg_norm(rawemb_passage)
        #     print(f"Raw embedded passage Norm: {rawemb_passage_norm}")
        #     projected_embedded_passage_norm = self.compute_avg_norm(projected_embedded_passage)
        #     print(f"Projected embedded passage Norm: {projected_embedded_passage_norm}")
        #     encoded_passage_norm = self.compute_avg_norm(encoded_passage)
        #     print(f"Encoded passage Norm: {encoded_passage_norm}")

        # Shape: (batch_size, question_length, passage_length)
        # Shape: (batch_size, question_length, passage_length)
        # if self.sim_key == 'dot':
        #     question_passage_similarity_phrase = self._executor_parameters.dotprod_matrix_attn(question_sim_repr,
        #                                                                                        passage_sim_repr)
        #     passage_question_similarity_phrase = question_passage_similarity_phrase.transpose(1, 2)
        # elif self.sim_key == 'bi':
        #     question_passage_similarity_phrase = self.q2p_bilinear_matrixattn(question_sim_repr,
        #                                                                       passage_sim_repr)
        #     passage_question_similarity_phrase = question_passage_similarity_phrase.transpose(1, 2)
        # elif self.sim_key == 'ma':

        # question_passage_similarity = self._matrix_attention(question_sim_repr, passage_sim_repr)
        # else:
        #     raise NotImplementedError

        # passage_question_similarity_phrase = self._matrix_attention(encoded_passage, encoded_question)
        # question_passage_similarity_phrase = passage_question_similarity_phrase.transpose(1, 2)
        #
        # # Shape: (batch_size, passage_length, question_length)
        # passage_question_attention_phrase = allenutil.masked_softmax(passage_question_similarity_phrase,
        #                                                              question_mask,
        #                                                              memory_efficient=True)
        #
        # # Shape: (batch_size, question_length, passage_length)
        # question_passage_attention_phrase = allenutil.masked_softmax(question_passage_similarity_phrase,
        #                                                              passage_mask,
        #                                                              memory_efficient=True)
        # if self.modeltype == "modeled":
        #     # Shape: (batch_size, passage_length, encoding_dim)
        #     passage_question_vectors = allenutil.weighted_sum(encoded_question, passage_question_attention_phrase)
        #     # Shape: (batch_size, passage_length, passage_length)
        #     attention_over_attention = torch.bmm(passage_question_attention_phrase, question_passage_attention_phrase)
        #     # Shape: (batch_size, passage_length, encoding_dim)
        #     passage_passage_vectors = allenutil.weighted_sum(encoded_passage, attention_over_attention)
        #
        #     # Shape: (batch_size, passage_length, encoding_dim * 4)
        #     merged_passage_attention_vectors = self._dropout(
        #         torch.cat([encoded_passage, passage_question_vectors,
        #                    encoded_passage * passage_question_vectors,
        #                    encoded_passage * passage_passage_vectors],
        #                   dim=-1)
        #     )
        #
        #     modeled_passage_input = self._modeling_proj_layer(merged_passage_attention_vectors)
        #     modeled_passage = self._dropout(self._modeling_layer(modeled_passage_input, passage_mask))
        #
        #     question_passage_similarity = self.qp_matrix_attention(encoded_question, modeled_passage)
        #     passage_question_similarity = question_passage_similarity.transpose(1, 2)
        #
        #     question_passage_attention = allenutil.masked_softmax(question_passage_similarity,
        #                                                           passage_mask.unsqueeze(1),
        #                                                           memory_efficient=True)
        #
        #     passage_question_attention = allenutil.masked_softmax(passage_question_similarity,
        #                                                           question_mask.unsqueeze(1),
        #                                                           memory_efficient=True)
        # elif self.modeltype == "encoded":
        #     modeled_passage = encoded_passage
        #     question_passage_similarity = question_passage_similarity_phrase
        #     passage_question_similarity = passage_question_similarity_phrase
        #     question_passage_attention = question_passage_attention_phrase
        #     passage_question_attention = passage_question_attention_phrase
        # else:
        #     raise NotImplementedError

        modeled_passage = encoded_passage
        passage_length = modeled_passage.size()[1]

        question_passage_similarity = self.qp_matrix_attention(encoded_question, modeled_passage)
        passage_question_similarity = question_passage_similarity.transpose(1, 2)

        question_passage_attention = allenutil.masked_softmax(question_passage_similarity,
                                                              passage_mask.unsqueeze(1),
                                                              memory_efficient=True)

        passage_question_attention = allenutil.masked_softmax(passage_question_similarity,
                                                              question_mask.unsqueeze(1),
                                                              memory_efficient=True)

        # ### Passage Token - Date Alignment
        # Shape: (batch_size, passage_length, passage_length)
        passage_passage_token2date_alignment = self.compute_token_date_alignments(
            modeled_passage=modeled_passage, passage_mask=passage_mask, passageidx2dateidx=passageidx2dateidx,
            passage_to_date_attention_params=self._executor_parameters.passage_to_date_attention)

        passage_passage_token2startdate_alignment = self.compute_token_date_alignments(
            modeled_passage=modeled_passage, passage_mask=passage_mask, passageidx2dateidx=passageidx2dateidx,
            passage_to_date_attention_params=self._executor_parameters.passage_to_start_date_attention)

        passage_passage_token2enddate_alignment = self.compute_token_date_alignments(
            modeled_passage=modeled_passage, passage_mask=passage_mask, passageidx2dateidx=passageidx2dateidx,
            passage_to_date_attention_params=self._executor_parameters.passage_to_end_date_attention)

        passage_tokenidx2dateidx_mask = (passageidx2numberidx > -1).float()

        # passage_passage_token2date_similarity = self._executor_parameters.passage_to_date_attention(modeled_passage,
        #                                                                                             modeled_passage)
        # passage_passage_token2date_similarity = passage_passage_token2date_similarity * passage_mask.unsqueeze(1)
        # passage_passage_token2date_similarity = passage_passage_token2date_similarity * passage_mask.unsqueeze(2)
        #
        # # Shape: (batch_size, passage_length) -- masking for number tokens in the passage
        # passage_tokenidx2dateidx_mask = (passageidx2dateidx > -1).float()
        # # Shape: (batch_size, passage_length, passage_length)
        # passage_passage_token2date_similarity = (passage_passage_token2date_similarity *
        #                                          passage_tokenidx2dateidx_mask.unsqueeze(1))
        # # Shape: (batch_size, passage_length, passage_length)
        # pasage_passage_token2date_aligment = allenutil.masked_softmax(passage_passage_token2date_similarity,
        #                                                               mask=passage_tokenidx2dateidx_mask.unsqueeze(1),
        #                                                               memory_efficient=True)
        # ### Passage Token - Num Alignment
        passage_passage_token2num_similarity = self._executor_parameters.passage_to_num_attention(modeled_passage,
                                                                                                  modeled_passage)
        passage_passage_token2num_similarity = passage_passage_token2num_similarity * passage_mask.unsqueeze(1)
        passage_passage_token2num_similarity = passage_passage_token2num_similarity * passage_mask.unsqueeze(2)

        # Shape: (batch_size, passage_length) -- masking for number tokens in the passage
        passage_tokenidx2numidx_mask = (passageidx2numberidx > -1).float()
        # Shape: (batch_size, passage_length, passage_length)
        passage_passage_token2num_similarity = (passage_passage_token2num_similarity *
                                                passage_tokenidx2numidx_mask.unsqueeze(1))
        # Shape: (batch_size, passage_length, passage_length)
        passage_passage_token2num_alignment = allenutil.masked_softmax(passage_passage_token2num_similarity,
                                                                       mask=passage_tokenidx2numidx_mask.unsqueeze(1),
                                                                       memory_efficient=True)

        # json_dicts = []
        # for i in range(batch_size):
        #     ques_tokens = metadata[i]['question_tokens']
        #     passage_tokens = metadata[i]['passage_tokens']
        #     p2num_sim = myutils.tocpuNPList(passage_passage_token2num_similarity[i])
        #     outdict = {'q': ques_tokens, 'p': passage_tokens, 'num': p2num_sim}
        #     json_dicts.append(outdict)
        # json.dump(json_dicts, num_attentions_f)
        # num_attentions_f.close()
        # exit()

        """ Aux Loss """
        if self.auxwinloss:
            inwindow_mask, outwindow_mask = self.masking_blockdiagonal(batch_size, passage_length,
                                                                       15, device_id)
            num_aux_loss = self.window_loss_numdate(passage_passage_token2num_alignment,
                                                    passage_tokenidx2numidx_mask,
                                                    inwindow_mask, outwindow_mask)

            date_aux_loss = self.window_loss_numdate(passage_passage_token2date_alignment,
                                                     passage_tokenidx2dateidx_mask,
                                                     inwindow_mask, outwindow_mask)

            start_date_aux_loss = self.window_loss_numdate(passage_passage_token2startdate_alignment,
                                                           passage_tokenidx2dateidx_mask,
                                                           inwindow_mask, outwindow_mask)

            end_date_aux_loss = self.window_loss_numdate(passage_passage_token2enddate_alignment,
                                                         passage_tokenidx2dateidx_mask,
                                                         inwindow_mask, outwindow_mask)

            # count_loss = self.number2count_auxloss(passage_number_values=passage_number_values,
            #                                        device_id=device_id)
            count_loss = 0.0

            aux_win_loss = num_aux_loss + date_aux_loss + count_loss + start_date_aux_loss + end_date_aux_loss

        else:
            aux_win_loss = 0.0

        """ Parser setup """
        # Shape: (B, encoding_dim)
        question_encoded_final_state = bert_pooled_out
        rawemb_question = encoded_question
        projected_embedded_question = encoded_question
        rawemb_passage = modeled_passage
        projected_embedded_passage = modeled_passage
        # question_encoded_final_state = allenutil.get_final_encoder_states(encoded_question,
        #                                                                   question_mask,
        #                                                                   self._phrase_layer.is_bidirectional())
        question_rawemb_aslist = [rawemb_question[i] for i in range(batch_size)]
        question_embedded_aslist = [projected_embedded_question[i] for i in range(batch_size)]
        question_encoded_aslist = [encoded_question[i] for i in range(batch_size)]
        question_mask_aslist = [question_mask[i] for i in range(batch_size)]
        passage_rawemb_aslist = [rawemb_passage[i] for i in range(batch_size)]
        passage_embedded_aslist = [projected_embedded_passage[i] for i in range(batch_size)]
        passage_encoded_aslist = [encoded_passage[i] for i in range(batch_size)]
        passage_modeled_aslist = [modeled_passage[i] for i in range(batch_size)]
        passage_mask_aslist = [passage_mask[i] for i in range(batch_size)]
        q2p_attention_aslist = [question_passage_attention[i] for i in range(batch_size)]
        p2q_attention_aslist = [passage_question_attention[i] for i in range(batch_size)]
        # p2pdate_similarity_aslist = [passage_passage_token2date_similarity[i] for i in range(batch_size)]
        # p2pnum_similarity_aslist = [passage_passage_token2num_similarity[i] for i in range(batch_size)]
        p2pdate_alignment_aslist = [passage_passage_token2date_alignment[i] for i in range(batch_size)]
        p2pstartdate_alignment_aslist = [passage_passage_token2startdate_alignment[i] for i in range(batch_size)]
        p2penddate_alignment_aslist = [passage_passage_token2enddate_alignment[i] for i in range(batch_size)]
        p2pnum_alignment_aslist = [passage_passage_token2num_alignment[i] for i in range(batch_size)]
        # passage_token2datetoken_sim_aslist = [passage_token2datetoken_similarity[i] for i in range(batch_size)]


        languages = [DropLanguage(rawemb_question=question_rawemb_aslist[i],
                                  embedded_question=question_embedded_aslist[i],
                                  encoded_question=question_encoded_aslist[i],
                                  rawemb_passage=passage_rawemb_aslist[i],
                                  embedded_passage=passage_embedded_aslist[i],
                                  encoded_passage=passage_encoded_aslist[i],
                                  modeled_passage=passage_modeled_aslist[i],
                                  # passage_token2datetoken_sim=None, #passage_token2datetoken_sim_aslist[i],
                                  question_mask=question_mask_aslist[i],
                                  passage_mask=passage_mask_aslist[i],
                                  passage_tokenidx2dateidx=passageidx2dateidx[i],
                                  passage_date_values=passage_date_values[i],
                                  passage_tokenidx2numidx=passageidx2numberidx[i],
                                  passage_num_values=passage_number_values[i],
                                  passage_number_sortedtokenidxs=passage_number_sortedtokenidxs[i],
                                  year_differences=year_differences[i],
                                  year_differences_mat=year_differences_mat[i],
                                  count_num_values=count_values[i],
                                  passagenum_differences=passagenumber_difference_values[i],
                                  passagenum_differences_mat=passagenumber_differences_mat[i],
                                  question_passage_attention=q2p_attention_aslist[i],
                                  passage_question_attention=p2q_attention_aslist[i],
                                  passage_token2date_alignment=p2pdate_alignment_aslist[i],
                                  passage_token2startdate_alignment=p2pstartdate_alignment_aslist[i],
                                  passage_token2enddate_alignment=p2penddate_alignment_aslist[i],
                                  passage_token2num_alignment=p2pnum_alignment_aslist[i],
                                  parameters=self._executor_parameters,
                                  start_types=None,  # batch_start_types[i],
                                  device_id=device_id,
                                  debug=self._debug,
                                  metadata=metadata[i]) for i in range(batch_size)]

        action2idx_map = {rule: i for i, rule in enumerate(languages[0].all_possible_productions())}
        idx2action_map = languages[0].all_possible_productions()

        """
        While training, we know the correct start-types for all instances and the gold-programs for some.
        For instances,
            #   with gold-programs, we should run a ConstrainedBeamSearch with target_sequences,
            #   with start-types, figure out the valid start-action-ids and run ConstrainedBeamSearch with firststep_allo..
        During Validation, we should **always** be running an un-constrained BeamSearch on the full language
        """
        mml_loss = 0
        if self.training:
            # If any instance is provided with goldprog, we need to divide the batch into supervised / unsupervised
            # and run fully-constrained decoding on supervised, and start-type-constrained-decoding on the rest
            if any(program_supervised):
                supervised_instances = [i for (i, ss) in enumerate(program_supervised) if ss is True]
                unsupervised_instances = [i for (i, ss) in enumerate(program_supervised) if ss is False]

                # List of (gold_actionseq_idxs, gold_actionseq_masks) -- for supervised instances
                supervised_gold_actionseqs = self._select_indices_from_list(gold_action_seqs, supervised_instances)
                s_gold_actionseq_idxs, s_gold_actionseq_masks = zip(*supervised_gold_actionseqs)
                s_gold_actionseq_idxs = list(s_gold_actionseq_idxs)
                s_gold_actionseq_masks = list(s_gold_actionseq_masks)
                (supervised_initial_state, _, _) = self.initialState_forInstanceIndices(supervised_instances,
                                                                                        languages, actions,
                                                                                        encoded_question,
                                                                                        question_mask,
                                                                                        question_encoded_final_state,
                                                                                        question_encoded_aslist,
                                                                                        question_mask_aslist)
                constrained_search = ConstrainedBeamSearch(self._beam_size,
                                                           allowed_sequences=s_gold_actionseq_idxs,
                                                           allowed_sequence_mask=s_gold_actionseq_masks)


                # print()
                # print(supervised_instances)
                # print(qtypes)
                # print(s_gold_actionseq_idxs)
                # for i in range(batch_size):
                #     print(metadata[i]["original_question"])
                # print()

                supervised_final_states = constrained_search.search(initial_state=supervised_initial_state,
                                                                    transition_function=self._decoder_step)

                for instance_states in supervised_final_states.values():
                    scores = [state.score[0].view(-1) for state in instance_states]
                    mml_loss += -allenutil.logsumexp(torch.cat(scores))
                mml_loss = mml_loss / len(supervised_final_states)

                if len(unsupervised_instances) > 0:
                    (unsupervised_initial_state, _, _) = self.initialState_forInstanceIndices(unsupervised_instances,
                                                                                              languages, actions,
                                                                                              encoded_question,
                                                                                              question_mask,
                                                                                              question_encoded_final_state,
                                                                                              question_encoded_aslist,
                                                                                              question_mask_aslist)

                    unsupervised_answer_types: List[List[str]] = self._select_indices_from_list(
                                                                                    answer_program_start_types,
                                                                                    unsupervised_instances)
                    unsupervised_ins_start_actionids: List[Set[int]] = self.get_valid_start_actionids(
                                                                            answer_types=unsupervised_answer_types,
                                                                            action2actionidx=action2idx_map)

                    firststep_constrained_search = MyConstrainedBeamSearch(self._beam_size)
                    unsup_final_states = firststep_constrained_search.search(num_steps=self._max_decoding_steps,
                                                                             initial_state=unsupervised_initial_state,
                                                                             transition_function=self._decoder_step,
                                                                             firststep_allowed_actions=\
                                                                                    unsupervised_ins_start_actionids,
                                                                             keep_final_unfinished_states=False)
                else:
                    unsup_final_states = []

                # Merge final_states for supervised and unsupervised instances
                best_final_states = self.merge_final_states(supervised_final_states,
                                                            unsup_final_states,
                                                            supervised_instances,
                                                            unsupervised_instances)

            else:
                (initial_state, _, _) = self.getInitialDecoderState(languages, actions, encoded_question,
                                                                    question_mask, question_encoded_final_state,
                                                                    question_encoded_aslist, question_mask_aslist,
                                                                    batch_size)
                batch_valid_start_actionids: List[Set[int]] = self.get_valid_start_actionids(
                                                                        answer_types=answer_program_start_types,
                                                                        action2actionidx=action2idx_map)
                search = MyConstrainedBeamSearch(self._beam_size)
                # Mapping[int, Sequence[StateType]])
                best_final_states = search.search(self._max_decoding_steps,
                                                  initial_state,
                                                  self._decoder_step,
                                                  firststep_allowed_actions=batch_valid_start_actionids,
                                                  keep_final_unfinished_states=False)
        else:
            (initial_state,
             _,
             _) = self.getInitialDecoderState(languages, actions, encoded_question,
                                              question_mask, question_encoded_final_state,
                                              question_encoded_aslist, question_mask_aslist,
                                              batch_size)
            # This is unconstrained beam-search
            best_final_states = self._decoder_beam_search.search(self._max_decoding_steps,
                                                                 initial_state,
                                                                 self._decoder_step,
                                                                 keep_final_unfinished_states=False)

        # batch_actionidxs: List[List[List[int]]]: All action sequence indices for each instance in the batch
        # batch_actionseqs: List[List[List[str]]]: All decoded action sequences for each instance in the batch
        # batch_actionseq_scores: List[List[torch.Tensor]]: Score for each program of each instance
        # batch_actionseq_probs: List[torch.FloatTensor]: Tensor containing normalized_prog_probs for each instance - no longer
        # batch_actionseq_sideargs: List[List[List[Dict]]]: List of side_args for each program of each instance
        # The actions here should be in the exact same order as passed when creating the initial_grammar_state ...
        # since the action_ids are assigned based on the order passed there.
        (batch_actionidxs,
         batch_actionseqs,
         batch_actionseq_scores,
         batch_actionseq_sideargs) = semparse_utils._convert_finalstates_to_actions(best_final_states=best_final_states,
                                                                                    possible_actions=actions,
                                                                                    batch_size=batch_size)

        # Adding Date-Comparison supervised event groundings to relevant actions
        max_passage_len = encoded_passage.size()[1]
        self.passage_attention_to_sidearg(qtypes, batch_actionseqs, batch_actionseq_sideargs, pattn_supervised,
                                          passage_attn_supervision, max_passage_len, device_id)

        self.datecompare_eventdategr_to_sideargs(qtypes,
                                                 batch_actionseqs,
                                                 batch_actionseq_sideargs,
                                                 datecomp_ques_event_date_groundings,
                                                 device_id)

        self.numcompare_eventnumgr_to_sideargs(qtypes,
                                               execution_supervised,
                                               batch_actionseqs,
                                               batch_actionseq_sideargs,
                                               numcomp_qspan_num_groundings,
                                               device_id)


        # # For printing predicted - programs
        # for idx, instance_progs in enumerate(batch_actionseqs):
        #     print(f"InstanceIdx:{idx}")
        #     print(metadata[idx]["question_tokens"])
        #     scores = batch_actionseq_scores[idx]
        #     for prog, score in zip(instance_progs, scores):
        #         print(f"{languages[idx].action_sequence_to_logical_form(prog)} : {score}")
        #         # print(f"{prog} : {score}")
        # print()

        # List[List[Any]], List[List[str]]: Denotations and their types for all instances
        batch_denotations, batch_denotation_types = self._get_denotations(batch_actionseqs,
                                                                          languages,
                                                                          batch_actionseq_sideargs)
        output_dict = {}
        # Computing losses if gold answers are given
        if answer_program_start_types is not None:
            # Execution losses --
            total_aux_loss = allenutil.move_to_device(torch.tensor(0.0), device_id).float()

            total_aux_loss += aux_win_loss
            if aux_win_loss != 0:
                self.auxwinloss_metric(aux_win_loss.item())

            if self.excloss:
                exec_loss = 0.0
                batch_exec_loss = 0.0
                execloss_normalizer = 0.0
                for ins_dens in batch_denotations:
                    for den in ins_dens:
                        execloss_normalizer += 1.0
                        exec_loss += den.loss
                if execloss_normalizer > 0:
                    batch_exec_loss = exec_loss / execloss_normalizer
                # This check is made explicit here since not all batches have this loss, hence a 0.0 value
                # only bloats the denominator in the metric. This is also done for other losses in below
                if batch_exec_loss != 0.0:
                    self.excloss_metric(batch_exec_loss.item())
                total_aux_loss += batch_exec_loss

            if self.qattloss:
                # Compute Question Attention Supervision auxiliary loss
                qattn_loss = self._ques_attention_loss(batch_actionseqs, batch_actionseq_sideargs, qtypes,
                                                       qattn_supervised, qattn_supervision)
                if qattn_loss != 0.0:
                    self.qattloss_metric(qattn_loss.item())
                total_aux_loss += qattn_loss
            if self.mmlloss:
                # This is computed above during beam search
                if mml_loss != 0.0:
                    self.mmlloss_metric(mml_loss.item())
                total_aux_loss += mml_loss

            if torch.isnan(total_aux_loss):
                logger.info(f"TotalAuxLoss is nan.")
                total_aux_loss = 0.0

            denotation_loss = allenutil.move_to_device(torch.tensor(0.0), device_id)
            batch_denotation_loss = allenutil.move_to_device(torch.tensor(0.0), device_id)
            if self.denotation_loss:
                for i in range(batch_size):
                    # Programs for an instance can be of multiple types;
                    # For each program, based on it's return type, we compute the log-likelihood
                    # against the appropriate gold-answer and add it to the instance_log_likelihood_list
                    # This is then weighed by the program-log-likelihood and added to the batch_loss

                    instance_prog_denotations, instance_prog_types = batch_denotations[i], batch_denotation_types[i]
                    instance_progs_logprob_list = batch_actionseq_scores[i]

                    # This instance does not have completed programs that were found in beam-search
                    if len(instance_prog_denotations) == 0:
                        continue

                    instance_log_likelihood_list = []
                    new_instance_progs_logprob_list = []
                    for progidx in range(len(instance_prog_denotations)):
                        denotation = instance_prog_denotations[progidx]
                        progtype = instance_prog_types[progidx]
                        prog_logprob = instance_progs_logprob_list[progidx]

                        if progtype == "PassageSpanAnswer":
                            # Tuple of start, end log_probs
                            denotation: PassageSpanAnswer = denotation
                            log_likelihood = self._get_span_answer_log_prob(answer_as_spans=answer_as_passage_spans[i],
                                                                            span_log_probs=denotation._value)

                            if torch.isnan(log_likelihood) == 1:
                                print(f"Batch index: {i}")
                                print(f"AnsAsPassageSpans:{answer_as_passage_spans[i]}")
                                print("\nPassageSpan Start and End logits")
                                print(denotation.start_logits)
                                print(denotation.end_logits)

                        elif progtype == "QuestionSpanAnswer":
                            # Tuple of start, end log_probs
                            denotation: QuestionSpanAnswer = denotation
                            log_likelihood = self._get_span_answer_log_prob(answer_as_spans=answer_as_question_spans[i],
                                                                            span_log_probs=denotation._value)
                            if torch.isnan(log_likelihood) == 1:
                                print(f"Batch index: {i}")
                                print(f"AnsAsQuestionSpans:{answer_as_question_spans[i]}")
                                print("\nQuestionSpan Start and End logits")
                                print(denotation.start_logits)
                                print(denotation.end_logits)

                        elif progtype == "YearDifference":
                            # Distribution over year_differences
                            denotation: YearDifference = denotation
                            pred_year_difference_dist = denotation._value
                            pred_year_diff_log_probs = torch.log(pred_year_difference_dist + 1e-40)
                            gold_year_difference_dist = allenutil.move_to_device(
                                                                    torch.FloatTensor(answer_as_year_difference[i]),
                                                                    cuda_device=device_id)
                            log_likelihood = torch.sum(pred_year_diff_log_probs * gold_year_difference_dist)

                        elif progtype == 'PassageNumber':
                            # Distribution over PassageNumbers
                            denotation: PassageNumber = denotation
                            pred_passagenumber_dist = denotation._value
                            pred_passagenumber_logprobs = torch.log(pred_passagenumber_dist + 1e-40)
                            gold_passagenum_dist = allenutil.move_to_device(
                                                                    torch.FloatTensor(answer_as_passage_number[i]),
                                                                    cuda_device=device_id)
                            log_likelihood = torch.sum(pred_passagenumber_logprobs * gold_passagenum_dist)

                        elif progtype == 'PassageNumberDifference':
                            denotation: PassageNumberDifference = denotation
                            pred_passagenumdiff_dist = denotation._value
                            pred_passagenumdiff_log_probs = torch.log(pred_passagenumdiff_dist + 1e-40)
                            gold_passagenum_difference_dist = allenutil.move_to_device(
                                torch.FloatTensor(answer_as_passagenum_difference[i]),
                                cuda_device=device_id)
                            log_likelihood = torch.sum(pred_passagenumdiff_log_probs * gold_passagenum_difference_dist)

                        elif progtype == 'CountNumber':
                            denotation: CountNumber = denotation
                            count_distribution = denotation._value
                            count_log_probs = torch.log(count_distribution + 1e-40)
                            gold_count_distribution = allenutil.move_to_device(torch.FloatTensor(answer_as_count[i]),
                                                                               cuda_device=device_id)
                            log_likelihood = torch.sum(count_log_probs * gold_count_distribution)

                        # Here implement losses for other program-return-types in else-ifs
                        else:
                            raise NotImplementedError
                        '''
                        elif progtype == "QuestionSpanAnswer":
                            denotation: QuestionSpanAnswer = denotation
                            log_likelihood = self._get_span_answer_log_prob(
                                answer_as_spans=answer_as_question_spans[i],answer_texts
                                span_log_probs=denotation._value)
                            if torch.isnan(log_likelihood) == 1:
                                print("\nQuestionSpan")
                                print(denotation.start_logits)
                                print(denotation.end_logits)
                                print(denotation._value)
                        '''
                        if torch.isnan(log_likelihood):
                            logger.info(f"Nan-loss encountered for denotation_log_likelihood")
                            log_likelihood = allenutil.move_to_device(torch.tensor(0.0), device_id)
                        if torch.isnan(prog_logprob):
                            logger.info(f"Nan-loss encountered for ProgType: {progtype}")
                            prog_logprob = allenutil.move_to_device(torch.tensor(0.0), device_id)

                        new_instance_progs_logprob_list.append(prog_logprob)
                        instance_log_likelihood_list.append(log_likelihood)

                    # Each is the shape of (number_of_progs,)
                    instance_denotation_log_likelihoods = torch.stack(instance_log_likelihood_list, dim=-1)
                    # instance_progs_log_probs = torch.stack(instance_progs_logprob_list, dim=-1)
                    instance_progs_log_probs = torch.stack(new_instance_progs_logprob_list, dim=-1)

                    allprogs_log_marginal_likelihoods = instance_denotation_log_likelihoods + instance_progs_log_probs
                    instance_marginal_log_likelihood = allenutil.logsumexp(allprogs_log_marginal_likelihoods)
                    # Added sum to remove empty-dim
                    instance_marginal_log_likelihood = torch.sum(instance_marginal_log_likelihood)
                    if torch.isnan(instance_marginal_log_likelihood):
                        logger.info(f"Nan-loss encountered for instance_marginal_log_likelihood")
                        logger.info(f"Prog log probs: {instance_progs_log_probs}")
                        logger.info(f"Instance denotation likelihoods: {instance_denotation_log_likelihoods}")

                        instance_marginal_log_likelihood = 0.0
                    denotation_loss += -1.0 * instance_marginal_log_likelihood

                    if torch.isnan(denotation_loss):
                        denotation_loss = 0.0

                batch_denotation_loss = denotation_loss / batch_size

            self.modelloss_metric(batch_denotation_loss.item())
            output_dict["loss"] = batch_denotation_loss + total_aux_loss

        # Get the predicted answers irrespective of loss computation.
        # For each program, given it's return type compute the predicted answer string
        if metadata is not None:
            output_dict["predicted_answer"] = []
            output_dict["all_predicted_answers"] = []

            for i in range(batch_size):
                original_question = metadata[i]["original_question"]
                original_passage = metadata[i]["original_passage"]
                question_token_offsets = metadata[i]["question_token_offsets"]
                passage_token_offsets = metadata[i]["passage_token_offsets"]
                instance_year_differences = year_differences[i]
                instance_passage_numbers = passage_number_values[i]
                instance_passagenum_diffs = passagenumber_difference_values[i]
                instance_count_values = count_values[i]
                answer_annotations = metadata[i]["answer_annotations"]

                instance_prog_denotations, instance_prog_types = batch_denotations[i], batch_denotation_types[i]
                instance_progs_logprob_list = batch_actionseq_scores[i]

                all_instance_progs_predicted_answer_strs: List[str] = []    # List of answers from diff instance progs
                for progidx in range(len(instance_prog_denotations)):
                    denotation = instance_prog_denotations[progidx]
                    progtype = instance_prog_types[progidx]
                    if progtype == "PassageSpanAnswer":
                        # Tuple of start, end log_probs
                        denotation: PassageSpanAnswer = denotation
                        # Shape: (2, ) -- start / end token ids
                        best_span = get_best_span(span_start_logits=denotation._value[0].unsqueeze(0),
                                                  span_end_logits=denotation._value[1].unsqueeze(0)).squeeze(0)

                        predicted_span = tuple(best_span.detach().cpu().numpy())
                        start_offset = passage_token_offsets[predicted_span[0]][0]
                        end_offset = passage_token_offsets[predicted_span[1]][1]
                        predicted_answer = original_passage[start_offset:end_offset]

                    elif progtype == "QuestionSpanAnswer":
                        # Tuple of start, end log_probs
                        denotation: QuestionSpanAnswer = denotation
                        # Shape: (2, ) -- start / end token ids
                        best_span = get_best_span(span_start_logits=denotation._value[0].unsqueeze(0),
                                                  span_end_logits=denotation._value[1].unsqueeze(0)).squeeze(0)

                        predicted_span = tuple(best_span.detach().cpu().numpy())
                        start_offset = question_token_offsets[predicted_span[0]][0]
                        end_offset = question_token_offsets[predicted_span[1]][1]
                        predicted_answer = original_question[start_offset:end_offset]

                    elif progtype == "YearDifference":
                        # Distribution over year_differences vector
                        denotation: YearDifference = denotation
                        year_differences_dist = denotation._value.detach().cpu().numpy()
                        predicted_yeardiff_idx = np.argmax(year_differences_dist)
                        # If not predicting year_diff = 0
                        # if predicted_yeardiff_idx == 0 and len(instance_year_differences) > 1:
                        #     predicted_yeardiff_idx = np.argmax(year_differences_dist[1:])
                        #     predicted_yeardiff_idx += 1
                        predicted_year_difference = instance_year_differences[predicted_yeardiff_idx]   # int
                        predicted_answer = str(predicted_year_difference)

                    elif progtype == "PassageNumber":
                        denotation: PassageNumber = denotation
                        predicted_passagenum_idx = torch.argmax(denotation._value).detach().cpu().numpy()
                        predicted_passage_number = instance_passage_numbers[predicted_passagenum_idx]   # int/float
                        predicted_passage_number = int(predicted_passage_number) if int(predicted_passage_number) == \
                                                                                    predicted_passage_number \
                                                                                        else predicted_passage_number
                        predicted_answer = str(predicted_passage_number)

                    elif progtype == 'PassageNumberDifference':
                        denotation: PassageNumberDifference = denotation
                        predicted_passagenumdiff_idx = torch.argmax(denotation._value).detach().cpu().numpy()
                        predicted_passagenum_diff = instance_passagenum_diffs[predicted_passagenumdiff_idx]
                        predicted_passagenum_diff = int(predicted_passagenum_diff) if int(predicted_passagenum_diff) == \
                                                                                      predicted_passagenum_diff \
                                                                                           else predicted_passagenum_diff
                        predicted_answer = str(predicted_passagenum_diff)

                    elif progtype == 'CountNumber':
                        denotation: CountNumber = denotation
                        count_idx = torch.argmax(denotation._value).detach().cpu().numpy()
                        predicted_count_answer = instance_count_values[count_idx]
                        predicted_count_answer = int(predicted_count_answer) if int(predicted_count_answer) == \
                                                                                predicted_count_answer \
                                                                                    else predicted_count_answer
                        predicted_answer = str(predicted_count_answer)

                    else:
                        raise NotImplementedError

                    all_instance_progs_predicted_answer_strs.append(predicted_answer)
                # If no program was found in beam-search
                if len(all_instance_progs_predicted_answer_strs) == 0:
                    all_instance_progs_predicted_answer_strs.append('')
                # Since the programs are sorted by decreasing scores, we can directly take the first pred answer
                instance_predicted_answer = all_instance_progs_predicted_answer_strs[0]
                output_dict["predicted_answer"].append(instance_predicted_answer)
                output_dict["all_predicted_answers"].append(all_instance_progs_predicted_answer_strs)

                answer_annotations = metadata[i].get('answer_annotations', [])
                self._drop_metrics(instance_predicted_answer, answer_annotations)

            if not self.training:
                output_dict['metadata'] = metadata
                # output_dict['best_span_ans_str'] = predicted_answers
                output_dict['answer_as_passage_spans'] = answer_as_passage_spans
                # output_dict['predicted_spans'] = batch_best_spans

                output_dict["batch_action_seqs"] = batch_actionseqs
                output_dict["batch_actionseq_scores"] = batch_actionseq_scores
                output_dict["batch_actionseq_sideargs"] = batch_actionseq_sideargs
                output_dict["languages"] = languages

        return output_dict


    def compute_token_date_alignments(self, modeled_passage, passage_mask, passageidx2dateidx,
                                      passage_to_date_attention_params):
        """Compute the passage_token-to-passage_date alignment matrix.

        Args:
        -----
            modeled_passage: (batch_size, passage_length, hidden_dim)
                Contextual passage repr.
            passage_mask: (batch_size, passage_length)
                Passage mask
            passageidx2dateidx: (batch_size, passage_length)
                For date-tokens, the index of the date-entity it belongs to, o/w masked with value = -1
            passage_to_date_attention_params: Some matrix-attention parameterization for computing the alignment matrix

        Returns:
        --------
            pasage_passage_token2date_aligment: (batch_size, passage_length, passage_length)
                Alignment matrix from passage_token (dim=1) to passage_date (dim=2)
                Should be masked in dim=2 for tokens that are not date-tokens
        """
        # ### Passage Token - Date Alignment
        # Shape: (batch_size, passage_length, passage_length)
        passage_passage_token2date_similarity = passage_to_date_attention_params(modeled_passage, modeled_passage)
        passage_passage_token2date_similarity = passage_passage_token2date_similarity * passage_mask.unsqueeze(1)
        passage_passage_token2date_similarity = passage_passage_token2date_similarity * passage_mask.unsqueeze(2)

        # Shape: (batch_size, passage_length) -- masking for number tokens in the passage
        passage_tokenidx2dateidx_mask = (passageidx2dateidx > -1).float()
        # Shape: (batch_size, passage_length, passage_length)
        passage_passage_token2date_similarity = (passage_passage_token2date_similarity *
                                                 passage_tokenidx2dateidx_mask.unsqueeze(1))
        # Shape: (batch_size, passage_length, passage_length)
        pasage_passage_token2date_aligment = allenutil.masked_softmax(passage_passage_token2date_similarity,
                                                                      mask=passage_tokenidx2dateidx_mask.unsqueeze(1),
                                                                      memory_efficient=True)
        return pasage_passage_token2date_aligment

    def compute_avg_norm(self, tensor):
        dim0_size = tensor.size()[0]
        dim1_size = tensor.size()[1]

        tensor_norm = tensor.norm(p=2, dim=2).sum() / (dim0_size * dim1_size)

        return tensor_norm

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        metric_dict = {}
        model_loss = self.modelloss_metric.get_metric(reset)
        exec_loss = self.excloss_metric.get_metric(reset)
        qatt_loss = self.qattloss_metric.get_metric(reset)
        mml_loss = self.mmlloss_metric.get_metric(reset)
        winloss = self.auxwinloss_metric.get_metric(reset)
        exact_match, f1_score = self._drop_metrics.get_metric(reset)
        metric_dict.update({'em': exact_match, 'f1': f1_score,
                            'ans': model_loss,
                            'exc': exec_loss,
                            'qatt': qatt_loss,
                            'mml': mml_loss,
                            'win': winloss})

        return metric_dict

    @staticmethod
    def _get_span_answer_log_prob(answer_as_spans: torch.LongTensor,
                                  span_log_probs: Tuple[torch.Tensor, torch.Tensor]) -> torch.Tensor:
        """ Compute the log_marginal_likelihood for the answer_spans given log_probs for start/end
            Compute log_likelihood (product of start/end probs) of each ans_span
            Sum the prob (logsumexp) for each span and return the log_likelihood

        Parameters:
        -----------
        answer: ``torch.LongTensor`` Shape: (number_of_spans, 2)
            These are the gold spans
        span_log_probs: ``torch.FloatTensor``
            2-Tuple with tensors of Shape: (length_of_sequence) for span_start/span_end log_probs

        Returns:
        log_marginal_likelihood_for_passage_span
        """

        # Unsqueezing dim=0 to make a batch_size of 1
        answer_as_spans = answer_as_spans.unsqueeze(0)

        span_start_log_probs, span_end_log_probs = span_log_probs
        span_start_log_probs = span_start_log_probs.unsqueeze(0)
        span_end_log_probs = span_end_log_probs.unsqueeze(0)

        # (batch_size, number_of_ans_spans)
        gold_passage_span_starts = answer_as_spans [:, :, 0]
        gold_passage_span_ends = answer_as_spans[:, :, 1]
        # Some spans are padded with index -1,
        # so we clamp those paddings to 0 and then mask after `torch.gather()`.
        gold_passage_span_mask = (gold_passage_span_starts != -1).long()
        clamped_gold_passage_span_starts = \
            allenutil.replace_masked_values(gold_passage_span_starts, gold_passage_span_mask, 0)
        clamped_gold_passage_span_ends = \
            allenutil.replace_masked_values(gold_passage_span_ends, gold_passage_span_mask, 0)
        # Shape: (batch_size, # of answer spans)
        log_likelihood_for_span_starts = \
            torch.gather(span_start_log_probs, 1, clamped_gold_passage_span_starts)
        log_likelihood_for_span_ends = \
            torch.gather(span_end_log_probs, 1, clamped_gold_passage_span_ends)
        # Shape: (batch_size, # of answer spans)
        log_likelihood_for_spans = \
            log_likelihood_for_span_starts + log_likelihood_for_span_ends
        # For those padded spans, we set their log probabilities to be very small negative value
        log_likelihood_for_spans = \
            allenutil.replace_masked_values(log_likelihood_for_spans, gold_passage_span_mask, -1e7)
        # Shape: (batch_size, )
        log_marginal_likelihood_for_span = allenutil.logsumexp(log_likelihood_for_spans)

        # Squeezing the batch-size 1
        log_marginal_likelihood_for_span = log_marginal_likelihood_for_span.squeeze(-1)

        return log_marginal_likelihood_for_span

    @staticmethod
    def get_valid_start_actionids(answer_types: List[List[str]],
                                  action2actionidx: Dict[str, int]) -> List[Set[int]]:
        """ For each instances, given answer_types as man-made strings, return the set of valid start action ids
            that return an object of that type.
            For example, given start_type as 'passage_span', '@start@ -> PassageSpanAnswer' is a valid start action
            See the reader for all possible values of start_types

            TODO(nitish): Instead of the reader passing in arbitrary strings and maintaining a mapping here,
            TODO(nitish): we can pass in the names of the LanguageObjects directly and make the start-action-str
                          in a programmatic manner.

            This is used while *training* to run constrained-beam-search to only search through programs for which
            the gold answer is known. These *** shouldn't *** beused during validation
        Returns:
        --------
        start_types: `List[Set[int]]`
            For each instance, a set of possible start_types
        """

        # Map from string passed by reader to LanguageType class
        answer_type_to_action_mapping = {'passage_span': '@start@ -> PassageSpanAnswer',
                                         'year_difference': '@start@ -> YearDifference',
                                         'passage_number': '@start@ -> PassageNumber',
                                         'question_span': '@start@ -> QuestionSpanAnswer',
                                         'count_number': '@start@ -> CountNumber'}
                                         #'passagenum_diff': '@start@ -> PassageNumberDifference'}

        valid_start_action_ids: List[Set[int]] = []
        for i in range(len(answer_types)):
            answer_program_types: List[str] = answer_types[i]
            start_action_ids = set()
            for start_type in answer_program_types:
                if start_type in answer_type_to_action_mapping:
                    actionstr = answer_type_to_action_mapping[start_type]
                    action_id = action2actionidx[actionstr]
                    start_action_ids.add(action_id)
                else:
                    logging.error(f"StartType: {start_type} has no valid action in {answer_type_to_action_mapping}")
            valid_start_action_ids.append(start_action_ids)

        return valid_start_action_ids


    def synthetic_num_grounding_loss(self,
                                     qtypes,
                                     synthetic_numgrounding_metadata,
                                     passage_passage_num_similarity,
                                     passageidx2numberidx):
        """
        Parameters:
        -----------
        passage_passage_num_similarity: (B, P, P)
        passage_tokenidx2dateidx: (B, P) containing non -1 values for number tokens. We'll use this for masking.
        synthetic_numgrounding_metadata: For each instance, list of (token_idx, number_idx), i.e.
            for row = token_idx, column = number_idx should be high
        """

        # (B, P)
        passageidx2numberidx_mask = (passageidx2numberidx > -1).float()

        # (B, P, P) -- with each row now normalized for number tokens
        passage_passage_num_attention = allenutil.masked_softmax(passage_passage_num_similarity,
                                                                 mask=passageidx2numberidx_mask.unsqueeze(1))
        log_likelihood = 0
        normalizer = 0
        for idx, (qtype, token_number_idx_pairs) in enumerate(zip(qtypes, synthetic_numgrounding_metadata)):
            if qtype == dropconstants.SYN_NUMGROUND_qtype:
                for token_idx, number_idx in token_number_idx_pairs:
                    log_likelihood += torch.log(passage_passage_num_attention[idx, token_idx, number_idx] + 1e-40)
                    normalizer += 1
        if normalizer > 0:
            log_likelihood = log_likelihood / normalizer

        loss = -1 * log_likelihood

        return loss


    def _ques_attention_loss(self,
                             batch_actionseqs: List[List[List[str]]],
                             batch_actionseq_sideargs: List[List[List[Dict]]],
                             qtypes: List[str],
                             qattn_supervised: List[bool],
                             qattn_supervision: torch.FloatTensor):

        """ Compute QAttn supervision loss for different kind of questions. Different question-types have diff.
            gold-programs and can have different number of qattn-supervision for each instance.
            There, the shape of qattn_supervision is (B, R, QLen) where R is the maximum number of attn-supervisions
            provided for an instance in this batch. For instances with less number of relevant actions
            the corresponding instance_slice will be padded with all zeros-tensors.

            We hard-code the question-types supported, and for each qtype, the relevant actions for which the
            qattn supervision will (should) be provided. For example, the gold program for date-comparison questions
            contains two 'PassageAttention -> find_PassageAttention' actions which use the question_attention sidearg
            for which the supervision is
             provided. Hence, qtype2relevant_actions_list - contains the two actions for the
            date-comparison question.

            The loss computed is the negative-log of sum of relevant probabilities.

            NOTE: This loss is only computed for instances that are marked as strongly-annotated and hence we don't
            check if the qattns-supervision needs masking.
        """
        find_passage_attention = 'PassageAttention -> find_PassageAttention'
        filter_passage_attention = '<PassageAttention:PassageAttention> -> filter_PassageAttention'
        relocate_passage_attention = '<PassageAttention:PassageAttention_answer> -> relocate_PassageAttention'

        single_find_passage_attention_list = [find_passage_attention]
        double_find_passage_attentions_list = [find_passage_attention, find_passage_attention]
        filter_find_passage_attention_list = [filter_passage_attention, find_passage_attention]
        relocate_find_passage_attention_list = [relocate_passage_attention, find_passage_attention]
        relocate_filterfind_passage_attention_list = [relocate_passage_attention,
                                                      filter_passage_attention,
                                                      find_passage_attention]

        qtypes_w_findPA = [dropconstants.NUM_find_qtype, dropconstants.MAX_find_qtype, dropconstants.MIN_find_qtype,
                           dropconstants.COUNT_find_qtype,
                           dropconstants.YARDS_findnum_qtype, dropconstants.YARDS_longest_qtype,
                           dropconstants.YARDS_shortest_qtype]

        qtypes_w_filterfindPA = [dropconstants.NUM_filter_find_qtype, dropconstants.MAX_filter_find_qtype,
                                 dropconstants.MIN_filter_find_qtype, dropconstants.COUNT_filter_find_qtype]

        qtypes_w_two_findPA = [dropconstants.DATECOMP_QTYPE, dropconstants.NUMCOMP_QTYPE]

        qtypes_w_relocatefindPA = [dropconstants.RELOC_find_qtype, dropconstants.RELOC_maxfind_qtype,
                                   dropconstants.RELOC_minfind_qtype]
        qtypes_w_relocate_filterfindPA = [dropconstants.RELOC_filterfind_qtype, dropconstants.RELOC_maxfilterfind_qtype,
                                          dropconstants.RELOC_minfilterfind_qtype]


        qtype2relevant_actions_list = {}

        for qtype in qtypes_w_findPA:
            qtype2relevant_actions_list[qtype] = single_find_passage_attention_list
        for qtype in qtypes_w_two_findPA:
            qtype2relevant_actions_list[qtype] = double_find_passage_attentions_list
        for qtype in qtypes_w_filterfindPA:
            qtype2relevant_actions_list[qtype] = filter_find_passage_attention_list
        for qtype in qtypes_w_relocatefindPA:
            qtype2relevant_actions_list[qtype] = relocate_find_passage_attention_list
        for qtype in qtypes_w_relocate_filterfindPA:
            qtype2relevant_actions_list[qtype] = relocate_filterfind_passage_attention_list

        loss = 0.0
        normalizer = 0

        for ins_idx in range(len(batch_actionseqs)):
            qattn_supervised_instance = qattn_supervised[ins_idx]
            if not qattn_supervised_instance:
                # no point even bothering
                continue
            qtype = qtypes[ins_idx]
            if qtype not in qtype2relevant_actions_list:
                continue

            instance_programs = batch_actionseqs[ins_idx]
            instance_prog_sideargs = batch_actionseq_sideargs[ins_idx]
            # Shape: (R, question_length)
            instance_qattn_supervision = qattn_supervision[ins_idx]
            # These are the actions for which qattn_supervision should be provided.
            relevant_actions = qtype2relevant_actions_list[qtype]
            num_relevant_actions = len(relevant_actions)
            for program, side_args in zip(instance_programs, instance_prog_sideargs):
                # Counter to keep a track of which relevant action we're looking for next
                relevant_action_idx = 0
                relevant_action = relevant_actions[relevant_action_idx]
                gold_qattn = instance_qattn_supervision[relevant_action_idx]
                for action, side_arg in zip(program, side_args):
                    if action == relevant_action:
                        question_attention = side_arg['question_attention']
                        if torch.sum(gold_qattn) != 0.0:
                            # Sum of probs -- model can distribute gold mass however it likes
                            # l = torch.sum(question_attention * gold_qattn)
                            # loss += torch.log(l)
                            # Prod of probs -- forces model to evenly distribute mass on gold-attn
                            log_question_attention = torch.log(question_attention + 1e-40)
                            l = torch.sum(log_question_attention * gold_qattn)
                            loss += l
                            normalizer += 1
                        else:
                            print(f"\nGold attention sum == 0.0."
                                  f"\nQattnSupervised: {qattn_supervised_instance}"
                                  f"\nQtype: {qtype}")
                        relevant_action_idx += 1

                        # All relevant actions for this instance in this program are found
                        if relevant_action_idx >= num_relevant_actions:
                            break
                        else:
                            relevant_action = relevant_actions[relevant_action_idx]
                            gold_qattn = instance_qattn_supervision[relevant_action_idx]
        if normalizer == 0:
            return loss
        else:
            return -1 * (loss/normalizer)


    @staticmethod
    def _get_best_spans(batch_denotations, batch_denotation_types,
                        question_char_offsets, question_strs, passage_char_offsets, passage_strs, *args):
        """ For all SpanType denotations, get the best span

        Parameters:
        ----------
        batch_denotations: List[List[Any]]
        batch_denotation_types: List[List[str]]
        """

        (question_mask_aslist, passage_mask_aslist) = args

        batch_best_spans = []
        batch_predicted_answers = []

        for instance_idx in range(len(batch_denotations)):
            instance_prog_denotations = batch_denotations[instance_idx]
            instance_prog_types = batch_denotation_types[instance_idx]

            instance_best_spans = []
            instance_predicted_ans = []

            for denotation, progtype in zip(instance_prog_denotations, instance_prog_types):
                # if progtype == "QuestionSpanAnswwer":
                # Distinction between QuestionSpanAnswer and PassageSpanAnswer is not needed currently,
                # since both classes store the start/end logits as a tuple
                # Shape: (2, )
                best_span = get_best_span(span_start_logits=denotation._value[0].unsqueeze(0),
                                          span_end_logits=denotation._value[1].unsqueeze(0)).squeeze(0)
                instance_best_spans.append(best_span)

                predicted_span = tuple(best_span.detach().cpu().numpy())
                if progtype == "QuestionSpanAnswer":
                    try:
                        start_offset = question_char_offsets[instance_idx][predicted_span[0]][0]
                        end_offset = question_char_offsets[instance_idx][predicted_span[1]][1]
                        predicted_answer = question_strs[instance_idx][start_offset:end_offset]
                    except:
                        print()
                        print(f"PredictedSpan: {predicted_span}")
                        print(f"QuesMaskLen: {question_mask_aslist[instance_idx].size()}")
                        print(f"StartLogProbs:{denotation._value[0]}")
                        print(f"EndLogProbs:{denotation._value[1]}")
                        print(f"LenofOffsets: {len(question_char_offsets[instance_idx])}")
                        print(f"QuesStrLen: {len(question_strs[instance_idx])}")

                elif progtype == "PassageSpanAnswer":
                    try:
                        start_offset = passage_char_offsets[instance_idx][predicted_span[0]][0]
                        end_offset = passage_char_offsets[instance_idx][predicted_span[1]][1]
                        predicted_answer = passage_strs[instance_idx][start_offset:end_offset]
                    except:
                        print()
                        print(f"PredictedSpan: {predicted_span}")
                        print(f"PassageMaskLen: {passage_mask_aslist[instance_idx].size()}")
                        print(f"LenofOffsets: {len(passage_char_offsets[instance_idx])}")
                        print(f"PassageStrLen: {len(passage_strs[instance_idx])}")
                else:
                    raise NotImplementedError

                instance_predicted_ans.append(predicted_answer)

            batch_best_spans.append(instance_best_spans)
            batch_predicted_answers.append(instance_predicted_ans)

        return batch_best_spans, batch_predicted_answers


    def passage_attention_to_sidearg(self,
                                     qtypes: List[str],
                                     batch_actionseqs: List[List[List[str]]],
                                     batch_actionseq_sideargs: List[List[List[Dict]]],
                                     pattn_supervised: List[bool],
                                     passage_attn_supervision: torch.FloatTensor,
                                     max_passage_len: int,
                                     device_id):
        """ If instance has passage attention supervision, add it to 'PassageAttention -> find_PassageAttention' """

        relevant_action = 'PassageAttention -> find_PassageAttention'
        for ins_idx in range(len(batch_actionseqs)):
            instance_programs = batch_actionseqs[ins_idx]
            instance_prog_sideargs = batch_actionseq_sideargs[ins_idx]
            instance_pattn_supervised = pattn_supervised[ins_idx]
            pattn_supervision = passage_attn_supervision[ins_idx]
            if not instance_pattn_supervised:
                pattn_supervision = None
            for program, side_args in zip(instance_programs, instance_prog_sideargs):
                for action, sidearg_dict in zip(program, side_args):
                    if action == relevant_action:
                        sidearg_dict['passage_attention'] = pattn_supervision

    def datecompare_eventdategr_to_sideargs(self,
                                            qtypes: List[str],
                                            batch_actionseqs: List[List[List[str]]],
                                            batch_actionseq_sideargs: List[List[List[Dict]]],
                                            datecomp_ques_event_date_groundings: List[Tuple[List[float], List[float]]],
                                            device_id):
        """ batch_event_date_groundings: For each question, a two-tuple containing the correct date-grounding for the
            two events mentioned in the question.
            These are in order of the annotation (order of events in question) but later the question attention
            might be predicted in reverse order and these will then be the wrong (reverse) annotations. Take care later.
        """
        # List[Tuple[torch.Tensor, torch.Tensor]]
        q_event_date_groundings = self.get_gold_question_event_date_grounding(datecomp_ques_event_date_groundings,
                                                                              device_id)

        relevant_action1 = '<PassageAttention,PassageAttention:PassageAttention_answer> -> compare_date_greater_than'
        relevant_action2 = '<PassageAttention,PassageAttention:PassageAttention_answer> -> compare_date_lesser_than'
        relevant_actions = [relevant_action1, relevant_action2]

        for ins_idx in range(len(batch_actionseqs)):
            instance_programs = batch_actionseqs[ins_idx]
            instance_prog_sideargs = batch_actionseq_sideargs[ins_idx]
            event_date_groundings = q_event_date_groundings[ins_idx]
            for program, side_args in zip(instance_programs, instance_prog_sideargs):
                for action, sidearg_dict in zip(program, side_args):
                    if action in relevant_actions:
                        sidearg_dict['event_date_groundings'] = event_date_groundings


    def numcompare_eventnumgr_to_sideargs(self,
                                          qtypes,
                                          execution_supervised,
                                          batch_actionseqs: List[List[List[str]]],
                                          batch_actionseq_sideargs: List[List[List[Dict]]],
                                          numcomp_qspan_num_groundings: List[Tuple[List[float], List[float]]],
                                          device_id):
        """ UPDATE: function name suggest only numpcomp, but works for other questions also
            numcomp_qspan_num_groundings - is a List of 1- or 2- or maybe n- tuple of number-grounding
        """
        """ batch_event_num_groundings: For each question, a 1- or 2--tuple containing the correct num-grounding for the
            two events mentioned in the question.
            
            Currently, for each qtype, we only have supervision for one of the actions, hence this function works
            (The tuple contains both groundings for the same action)
            If we need somthing like qattn, where multiple supervisions are provided, things will have to change
        """
        # Reusing the function written for dates -- should work fine
        # List[Tuple[torch.Tensor, torch.Tensor]]
        q_event_num_groundings = self.get_gold_question_event_date_grounding(numcomp_qspan_num_groundings,
                                                                             device_id)
        numcomp_action_gt = '<PassageAttention,PassageAttention:PassageAttention_answer> -> compare_num_greater_than'
        numcomp_action_lt = '<PassageAttention,PassageAttention:PassageAttention_answer> -> compare_num_greater_than'
        findnum_action = '<PassageAttention:PassageNumber> -> find_PassageNumber'
        maxNumPattn_action = '<PassageAttention:PassageAttention> -> maxNumPattn'
        minNumPattn_action = '<PassageAttention:PassageAttention> -> minNumPattn'

        qtype2relevant_actions_list = {
                                        dropconstants.NUMCOMP_QTYPE: [numcomp_action_gt, numcomp_action_lt],
                                        dropconstants.NUM_find_qtype: [findnum_action],
                                        dropconstants.NUM_filter_find_qtype: [findnum_action],
                                        dropconstants.MAX_find_qtype: [maxNumPattn_action],
                                        dropconstants.MAX_filter_find_qtype: [maxNumPattn_action],
                                        dropconstants.MIN_find_qtype: [minNumPattn_action],
                                        dropconstants.MIN_filter_find_qtype: [minNumPattn_action],
                                      }

        for ins_idx in range(len(batch_actionseqs)):
            instance_programs = batch_actionseqs[ins_idx]
            instance_prog_sideargs = batch_actionseq_sideargs[ins_idx]
            event_num_groundings = q_event_num_groundings[ins_idx]
            qtype = qtypes[ins_idx]    # Could be UNK
            if qtype not in qtype2relevant_actions_list:
                continue
            relevant_actions = qtype2relevant_actions_list[qtype]
            for program, side_args in zip(instance_programs, instance_prog_sideargs):
                for action, sidearg_dict in zip(program, side_args):
                    if action in relevant_actions:
                        sidearg_dict['event_num_groundings'] = event_num_groundings


    def get_gold_question_event_date_grounding(self,
                                               question_event_date_groundings: List[Tuple[List[int], List[int]]],
                                               device_id: int) -> List[Tuple[torch.Tensor, torch.Tensor]]:
        """ Converts input event date groundings (date-comparison) to FloatTensors """
        question_date_groundings = []
        # for grounding_1, grounding_2 in question_event_date_groundings:
        #     g1 = allenutil.move_to_device(torch.FloatTensor(grounding_1), device_id)
        #     g2 = allenutil.move_to_device(torch.FloatTensor(grounding_2), device_id)
        #     question_date_groundings.append((g1, g2))

        # Reader passes two groundings if not provided, hence all elements have tensors and no need to check for None
        for groundings in question_event_date_groundings:
            groundings_tensors = []
            for grounding in groundings:
                g = allenutil.move_to_device(torch.FloatTensor(grounding), device_id)
                groundings_tensors.append(g)
            question_date_groundings.append(groundings_tensors)
        return question_date_groundings


    def passageAnsSpan_to_PassageAttention(self, answer_as_passage_spans, passage_mask):
        """ Convert answers as passage span into passage attention for model introspection

        Parameters:
        ----------
        answer_as_passage_spans: `torch.Tensor`
            Tensor of shape (batch_size, number_of_ans_spans, 2) containing start / end positions
        passage_mask: `torch.FloatTensor`
            Tensor of shape (batch_size, passage_length)

        Returns:
        --------
        attention: `torch.FloatTensor`
            List of (passage_length, ) shaped tensor containing normalized attention for gold spans
        """
        # Shape: (batch_size, number_of_ans_spans, 2)
        answer_as_spans = answer_as_passage_spans.long()

        # TODO(nitish): ONLY USING FIRST CORRECT SPAN OUT OF MULTIPLE POSSIBLE
        # answer_as_spans = answer_as_spans[:, 0, :].unsqueeze(1)

        # Shape: (batch_size, number_of_ans_spans)
        span_starts = answer_as_spans[:, :, 0]
        span_ends = answer_as_spans[:, :, 1]
        answers_mask = (span_starts >= 0).float()

        # Shape: (batch_size, 1, number_of_ans_spans)
        span_starts_ex = span_starts.unsqueeze(1)
        span_ends_ex = span_ends.unsqueeze(1)

        # Idea: Make a range vector from 0 <-> seq_len - 1 and convert into boolean with (val > start) and (val < end)
        # Such items in the sequence are within the span range
        # Shape: (passage_length, )
        range_vector = allenutil.get_range_vector(passage_mask.size(1), allenutil.get_device_of(passage_mask))

        # Shape: (1, passage_length, 1)
        range_vector = range_vector.unsqueeze(0).unsqueeze(2)

        # Shape: (batch_size, passage_length, number_of_ans_spans) - 1 as tokens in the span, 0 otherwise
        span_range_mask = (range_vector >= span_starts_ex).float() * (range_vector <= span_ends_ex).float()
        span_range_mask = span_range_mask * answers_mask.unsqueeze(1)

        # Shape: (batch_size, passage_length)
        unnormalized_attention = span_range_mask.sum(2)
        normalized_attention = unnormalized_attention / unnormalized_attention.sum(1, keepdim=True)

        attention_aslist = [normalized_attention[i, :] for i in range(normalized_attention.size(0))]

        return attention_aslist


    def passageattn_to_startendlogits(self, passage_attention, passage_mask):
        span_start_logits = passage_attention.new_zeros(passage_attention.size())
        span_end_logits = passage_attention.new_zeros(passage_attention.size())

        nonzeroindcs = (passage_attention > 0).nonzero()

        startidx = nonzeroindcs[0]
        endidx = nonzeroindcs[-1]

        print(f"{startidx} {endidx}")

        span_start_logits[startidx] = 2.0
        span_end_logits[endidx] = 2.0

        span_start_logits = allenutil.replace_masked_values(span_start_logits, passage_mask, -1e32)
        span_end_logits = allenutil.replace_masked_values(span_end_logits, passage_mask, -1e32)

        span_start_logits += 1e-7
        span_end_logits += 1e-7

        return (span_start_logits, span_end_logits)


    def passage_ans_attn_to_sideargs(self,
                                     batch_actionseqs: List[List[List[str]]],
                                     batch_actionseq_sideargs: List[List[List[Dict]]],
                                     batch_gold_attentions: List[torch.Tensor]):

        for ins_idx in range(len(batch_actionseqs)):
            instance_programs = batch_actionseqs[ins_idx]
            instance_prog_sideargs = batch_actionseq_sideargs[ins_idx]
            instance_gold_attention = batch_gold_attentions[ins_idx]
            for program, side_args in zip(instance_programs, instance_prog_sideargs):
                first_qattn = True   # This tells model which qent attention to use
                # print(side_args)
                # print()
                for action, sidearg_dict in zip(program, side_args):
                    if action == 'PassageSpanAnswer -> find_passageSpanAnswer':
                        sidearg_dict['passage_attention'] = instance_gold_attention


    def getInitialDecoderState(self,
                               languages: List[DropLanguage],
                               actions: List[List[ProductionRule]],
                               encoded_question: torch.FloatTensor,
                               question_mask: torch.FloatTensor,
                               question_encoded_final_state: torch.FloatTensor,
                               question_encoded_aslist: List[torch.Tensor],
                               question_mask_aslist: List[torch.Tensor],
                               batch_size: int):
        # List[torch.Tensor(0.0)] -- Initial log-score list for the decoding
        initial_score_list = [encoded_question.new_zeros(1, dtype=torch.float) for _ in range(batch_size)]

        initial_grammar_statelets = []
        batch_action2actionidx: List[Dict[str, int]] = []
        # This is kind of useless, only needed for debugging in BasicTransitionFunction
        batch_actionidx2actionstr: List[List[str]] = []

        for i in range(batch_size):
            (grammar_statelet,
             action2actionidx,
             actionidx2actionstr) = self._create_grammar_statelet(languages[i], actions[i])

            initial_grammar_statelets.append(grammar_statelet)
            batch_actionidx2actionstr.append(actionidx2actionstr)
            batch_action2actionidx.append(action2actionidx)

        initial_rnn_states = self._get_initial_rnn_state(question_encoded=encoded_question,
                                                         question_mask=question_mask,
                                                         question_encoded_finalstate=question_encoded_final_state,
                                                         question_encoded_aslist=question_encoded_aslist,
                                                         question_mask_aslist=question_mask_aslist)

        initial_side_args = [[] for _ in range(batch_size)]

        # Initial grammar state for the complete batch
        initial_state = GrammarBasedState(batch_indices=list(range(batch_size)),
                                          action_history=[[] for _ in range(batch_size)],
                                          score=initial_score_list,
                                          rnn_state=initial_rnn_states,
                                          grammar_state=initial_grammar_statelets,
                                          possible_actions=actions,
                                          extras=batch_actionidx2actionstr,
                                          debug_info=initial_side_args)

        return (initial_state, batch_action2actionidx, batch_actionidx2actionstr)


    def _select_indices_from_list(self, list, indices):
        new_list = [list[i] for i in indices]
        return new_list

    def initialState_forInstanceIndices(self,
                                        instances_list: List[int],
                                        languages: List[DropLanguage],
                                        actions: List[List[ProductionRule]],
                                        encoded_question: torch.FloatTensor,
                                        question_mask: torch.FloatTensor,
                                        question_encoded_final_state: torch.FloatTensor,
                                        question_encoded_aslist: List[torch.Tensor],
                                        question_mask_aslist: List[torch.Tensor]):

        s_languages = self._select_indices_from_list(languages, instances_list)
        s_actions = self._select_indices_from_list(actions, instances_list)
        s_encoded_question = encoded_question[instances_list]
        s_question_mask = question_mask[instances_list]
        s_question_encoded_final_state = question_encoded_final_state[instances_list]
        s_question_encoded_aslist = self._select_indices_from_list(question_encoded_aslist, instances_list)
        s_question_mask_aslist = self._select_indices_from_list(question_mask_aslist, instances_list)

        num_instances = len(instances_list)

        return self.getInitialDecoderState(s_languages, s_actions, s_encoded_question, s_question_mask,
                                           s_question_encoded_final_state, s_question_encoded_aslist,
                                           s_question_mask_aslist, num_instances)


    def merge_final_states(self,
                           supervised_final_states, unsupervised_final_states,
                           supervised_instances: List[int], unsupervised_instances: List[int]):

        """ Supervised and unsupervised final_states are dicts with keys in order from 0 - len(dict)
            The final keys are the instances' batch index which is stored in (un)supervised_instances list
            i.e. index = supervised_instances[0] is the index of the instance in the original batch
            whose final state is now in supervised_final_states[0].
            Therefore the final_state should contain; final_state[index] = supervised_final_states[0]
        """

        if len(supervised_instances) == 0:
            return unsupervised_final_states

        if len(unsupervised_instances) == 0:
            return supervised_final_states

        batch_size = len(supervised_instances) + len(unsupervised_instances)

        final_states = {}

        for i in range(batch_size):
            if i in supervised_instances:
                idx = supervised_instances.index(i)
                state_value = supervised_final_states[idx]
                final_states[i] = state_value
            else:
                idx = unsupervised_instances.index(i)
                # Unsupervised instances go through beam search and not always is a program found for them
                # If idx does not exist in unsupervised_final_states, don't add in final_states
                # Only add a instance_idx if it exists in final states -- not all beam-searches result in valid-paths
                if idx in unsupervised_final_states:
                    state_value = unsupervised_final_states[idx]
                    final_states[i] = state_value

        return final_states

    def aux_count_loss(self, passage_attention, passage_mask, answer_as_count, count_mask):
        if torch.sum(count_mask) == 0:
            loss, accuracy = 0.0, 0.0
            return loss, accuracy

        batch_size = passage_attention.size()[0]
        # List of (B, P) shaped tensors
        scaled_attentions = [passage_attention * sf for sf in self._executor_parameters.passage_attention_scalingvals]
        # Shape: (B, passage_length, num_scaling_factors)
        scaled_passage_attentions = torch.stack(scaled_attentions, dim=2)
        # Shape: (B, hidden_dim)
        count_hidden_repr = self._executor_parameters.passage_attention_to_count(scaled_passage_attentions,
                                                                                 passage_mask)
        # Shape: (B, num_counts)
        passage_span_logits = self._executor_parameters.passage_count_predictor(count_hidden_repr)
        count_distribution = torch.softmax(passage_span_logits, dim=1)

        loss = 0
        accuracy = 0
        if answer_as_count is not None:
            # (B, num_counts)
            answer_as_count = answer_as_count.float()
            count_log_probs = torch.log(count_distribution + 1e-40)
            log_likelihood = torch.sum(count_log_probs * answer_as_count * count_mask.unsqueeze(1).float())

            loss = -1 * log_likelihood
            loss = loss / torch.sum(count_mask).float()

            # List of predicted count idxs
            count_idx = torch.argmax(count_distribution, 1)
            gold_count_idxs = torch.argmax(answer_as_count, 1)
            correct_vec = (count_idx == gold_count_idxs).float() * count_mask.float()
            accuracy = (torch.sum(correct_vec) / torch.sum(count_mask)).detach().cpu().numpy()

        return loss, accuracy

    def masking_blockdiagonal(self, batch_size, passage_length, window, device_id):
        """ Make a (batch_size, passage_length, passage_length) tensor M of 1 and -1 in which for each row x,
            M[:, x, y] = -1 if y < x - window or y > x + window, else it is 1.
            Basically for the x-th row, the [x-win, x+win] columns should be 1, and rest -1
        """

        lower_limit = [max(0, i - window) for i in range(passage_length)]
        upper_limit = [min(passage_length, i + window) for i in range(passage_length)]

        # Tensors of lower and upper limits for each row
        lower = allenutil.move_to_device(torch.LongTensor(lower_limit), cuda_device=device_id)
        upper = allenutil.move_to_device(torch.LongTensor(upper_limit), cuda_device=device_id)
        lower_un = lower.unsqueeze(1)
        upper_un = upper.unsqueeze(1)

        # Range vector for each row
        lower_range_vector = allenutil.get_range_vector(passage_length, device=device_id).unsqueeze(0)
        upper_range_vector = allenutil.get_range_vector(passage_length, device=device_id).unsqueeze(0)

        # Masks for lower and upper limits of the mask
        lower_mask = lower_range_vector >= lower_un
        upper_mask = upper_range_vector <= upper_un

        # Final-mask that we require
        inwindow_mask = (lower_mask == upper_mask).float()
        outwindow_mask = (lower_mask != upper_mask).float()

        return inwindow_mask, outwindow_mask


    def window_loss_numdate(self, passage_passage_alignment, passage_tokenidx_mask,
                            inwindow_mask, outwindow_mask):
        """
        The idea is to first softmax the similarity_scores to get a distribution over the date/num tokens from each
        passage token.

        For each passage_token,
            -- increase the sum of prob for date/num tokens around it in a window (don't know which date/num is correct)
            -- decrease the prod of prob for date/num tokens outside the window (know that all date/num are wrong)

        Parameters:
        -----------
        passage_passage_similarity_scores: (batch_size, passage_length, passage_length)
            For each passage_token, a similarity score to other passage_tokens for data/num
            This should ideally, already be masked

        passage_tokenidx_mask: (batch_size, passage_length)
            Mask for tokens that are num/date

        inwindow_mask: (passage_length, passage_length)
            For row x, inwindow_mask[x, x-window : x+window] = 1 and 0 otherwise. Mask for a window around the token
        outwindow_mask: (passage_length, passage_length)
            Opposite of inwindow_mask. For each row x, the columns are 1 outside of a window around x
        """
        # # (batch_size, passage_length, passage_length)
        # passage_passage_alignment_matrix = allenutil.masked_softmax(passage_passage_similarity_scores,
        #                                                             passage_tokenidx_mask.unsqueeze(1),
        #                                                             memory_efficient=True)


        inwindow_mask = inwindow_mask.unsqueeze(0) * passage_tokenidx_mask.unsqueeze(1)
        inwindow_probs = passage_passage_alignment * inwindow_mask
        # This signifies that each token can distribute it's prob to nearby-date/num in anyway
        # Shape: (batch_size, passage_length)
        sum_inwindow_probs = inwindow_probs.sum(2)
        mask_sum = (inwindow_mask.sum(2) > 0).float()
        # Image a row where mask = 0, there sum of probs will be zero and we need to compute masked_log
        masked_sum_inwindow_probs = allenutil.replace_masked_values(sum_inwindow_probs, mask_sum, replace_with=1e-40)
        log_sum_inwindow_probs = torch.log(masked_sum_inwindow_probs + 1e-40) * mask_sum
        inwindow_likelihood = torch.sum(log_sum_inwindow_probs)
        if torch.sum(inwindow_mask) > 0:
            inwindow_likelihood = inwindow_likelihood / torch.sum(inwindow_mask)
        else:
            inwindow_likelihood = 0.0

        outwindow_mask = outwindow_mask.unsqueeze(0) * passage_tokenidx_mask.unsqueeze(1)
        # For tokens outside the window, increase entropy of the distribution. i.e. -\sum p*log(p)
        # Since we'd like to distribute the weight equally to things outside the window
        # Shape: (batch_size, passage_length, passage_length)
        outwindow_probs = passage_passage_alignment * outwindow_mask

        masked_outwindow_probs = allenutil.replace_masked_values(outwindow_probs, outwindow_mask, replace_with=1e-40)
        outwindow_probs_log = torch.log(masked_outwindow_probs + 1e-40) * outwindow_mask
        # Shape: (batch_length, passage_length)
        outwindow_negentropies = torch.sum(outwindow_probs * outwindow_probs_log)

        if torch.sum(outwindow_mask) > 0:
            outwindow_negentropies = outwindow_negentropies / torch.sum(outwindow_mask)
        else:
            outwindow_negentropies = 0.0

            # Increase inwindow likelihod and decrease outwindow-negative-entropy
        loss = -1 * inwindow_likelihood + outwindow_negentropies

        return loss

    def number2count_auxloss(self, passage_number_values: List[List[float]], device_id=-1):
        """ Using passage numnbers, make a (batch_size, max_passage_numbers) (padded) tensor, each containing a
            noisy distribution with mass distributed over x-numbers. The corresponding count-answer will be x.
            Use the attention2count rnn to predict a count value and compute the loss.
        """
        batch_size = len(passage_number_values)
        # List of length -- batch-size
        num_of_passage_numbers = [len(nums) for nums in passage_number_values]
        max_passage_numbers = max(num_of_passage_numbers)

        # Shape: (batch_size, )
        num_pasasge_numbers = allenutil.move_to_device(torch.LongTensor(num_of_passage_numbers), cuda_device=device_id)
        # Shape: (max_passage_numbers, )
        range_vector = allenutil.get_range_vector(size=max_passage_numbers, device=device_id)

        # Shape: (batch_size, maxnum_passage_numbers)
        mask = (range_vector.unsqueeze(0) < num_pasasge_numbers.unsqueeze(1)).float()

        number_distributions = mask.new_zeros(batch_size, max_passage_numbers).normal_(0, 0.05).abs_()
        count_answers = number_distributions.new_zeros(batch_size).long()
        for i, num_numbers in enumerate(num_of_passage_numbers):
            """ Sample a count value between [0, min(5, num_numbers)]. Sample indices in this range, and set them as 1.
                Add gaussian noise to the whole tensor and normalize. 
            """
            # Pick a count answer
            count_value = random.randint(1, min(7, num_numbers))
            count_answers[i] = count_value
            # Pick the indices that will have mass
            if count_value > 0:
                indices = random.sample(range(num_numbers), count_value)
                # Add 1.0 to all sampled indices
                number_distributions[i, indices] += 1.0

        number_distributions = number_distributions * mask
        # Shape: (batch_size, maxnum_passage_numbers)
        number_distributions = number_distributions / torch.sum(number_distributions, dim=1).unsqueeze(1)

        # Distributions made; computing loss
        scaled_attentions = [number_distributions * sf for sf in
                             self._executor_parameters.passage_attention_scalingvals]
        # Shape: (batch_size, maxnum_passage_numbers, num_scaling_factors)
        stacked_scaled_attentions = torch.stack(scaled_attentions, dim=2)

        # Shape: (batch_size, hidden_dim)
        count_hidden_repr = self.passage_attention_to_count(stacked_scaled_attentions, mask)

        # Shape: (batch_size, num_counts)
        count_logits = self.passage_count_predictor(count_hidden_repr)

        count_loss = F.cross_entropy(input=count_logits, target=count_answers)

        return count_loss













